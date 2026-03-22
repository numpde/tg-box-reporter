from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping, Pattern


def _format_duration(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes}m{secs}s"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"


def _utc_from_epoch(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify_status(status: object) -> str | None:
    if not isinstance(status, int):
        return None
    if status < 100 or status > 599:
        return None
    return f"{status // 100}xx"


@dataclass(frozen=True)
class RouteAlertKey:
    source: str
    env: str
    method: str
    route: str

    def dedupe_suffix(self) -> str:
        return f"{self.source}:{self.env}:{self.method}:{self.route}"


@dataclass(frozen=True)
class RouteErrorRateHighConfig:
    enabled: bool = False
    allowlist_regex: Pattern[str] | None = None
    window_seconds: int = 300
    min_requests: int = 10
    min_errors: int = 5
    error_rate_gt: float = 0.5
    clear_rate_lt: float = 0.2
    status_classes: tuple[str, ...] = ("5xx",)

    def matches_route(self, route: str) -> bool:
        return self.allowlist_regex is None or bool(self.allowlist_regex.search(route))


@dataclass(frozen=True)
class RouteSeenAfterQuietConfig:
    enabled: bool = False
    allowlist_regex: Pattern[str] | None = None
    quiet_period_seconds: int = 21600
    emit_on_first_seen: bool = False

    def matches_route(self, route: str) -> bool:
        return self.allowlist_regex is None or bool(self.allowlist_regex.search(route))


@dataclass(frozen=True)
class CollectorAlertsConfig:
    enabled: bool = False
    max_recent: int = 200
    retention_seconds: int = 86400
    route_error_rate_high: RouteErrorRateHighConfig = RouteErrorRateHighConfig()
    route_seen_after_quiet: RouteSeenAfterQuietConfig = RouteSeenAfterQuietConfig()

    @property
    def state_retention_seconds(self) -> int:
        return max(
            3600,
            self.route_error_rate_high.window_seconds * 2,
            self.route_seen_after_quiet.quiet_period_seconds * 2,
        )


@dataclass
class _RouteErrorWindowState:
    events: deque[tuple[float, str | None, str, int | None]] = field(default_factory=deque)
    open: bool = False
    opened_at_utc: str | None = None
    last_seen_at: float = 0.0


@dataclass
class _RouteQuietState:
    has_seen: bool = False
    last_seen_at: float | None = None


def build_route_alert_key(event: Mapping[str, object]) -> RouteAlertKey | None:
    route = str(event.get("route") or "").strip()
    method = str(event.get("method") or "").strip()
    if not route or not method:
        return None
    return RouteAlertKey(
        source=str(event.get("source") or "<source>"),
        env=str(event.get("env") or "<env>"),
        method=method,
        route=route,
    )


class AlertRuleEngine:
    def __init__(
        self,
        config: CollectorAlertsConfig,
        *,
        now_utc: Callable[[], str],
    ):
        self.config = config
        self.now_utc = now_utc
        self._error_states: dict[RouteAlertKey, _RouteErrorWindowState] = {}
        self._quiet_states: dict[RouteAlertKey, _RouteQuietState] = {}

    def evaluate(self, event: Mapping[str, object], *, now: float) -> list[dict[str, object]]:
        if not self.config.enabled:
            return []
        key = build_route_alert_key(event)
        if key is None:
            return []

        alerts: list[dict[str, object]] = []
        alerts.extend(self._evaluate_route_error_rate_high(key, event, now=now))
        alerts.extend(self._evaluate_route_seen_after_quiet(key, event, now=now))
        return alerts

    def prune(self, *, now: float) -> None:
        cutoff = now - float(self.config.state_retention_seconds)

        for key, state in list(self._error_states.items()):
            self._prune_error_window(state, now=now)
            if not state.events and state.last_seen_at < cutoff:
                self._error_states.pop(key, None)

        for key, state in list(self._quiet_states.items()):
            if state.last_seen_at is None or state.last_seen_at < cutoff:
                self._quiet_states.pop(key, None)

    def _evaluate_route_error_rate_high(
        self,
        key: RouteAlertKey,
        event: Mapping[str, object],
        *,
        now: float,
    ) -> list[dict[str, object]]:
        rule = self.config.route_error_rate_high
        if not rule.enabled or not rule.matches_route(key.route):
            return []

        state = self._error_states.setdefault(key, _RouteErrorWindowState())
        status_class = classify_status(event.get("status"))
        latest_status = event.get("status")
        event_ts = str(event.get("ts") or event.get("received_at_utc") or self.now_utc())
        state.events.append((now, status_class, event_ts, latest_status if isinstance(latest_status, int) else None))
        state.last_seen_at = now
        self._prune_error_window(state, now=now)

        total = len(state.events)
        error_count = sum(1 for _, cls, _, _ in state.events if cls in rule.status_classes)
        error_rate = (float(error_count) / float(total)) if total else 0.0
        latest_status_code = next(
            (status for _, _, _, status in reversed(state.events) if isinstance(status, int)),
            None,
        )
        first_seen_utc = state.events[0][2] if state.events else event_ts
        last_seen_utc = state.events[-1][2] if state.events else event_ts

        stats = {
            "window_seconds": rule.window_seconds,
            "total_requests": total,
            "error_requests": error_count,
            "error_rate": round(error_rate, 3),
            "latest_status": latest_status_code,
            "status_classes": list(rule.status_classes),
            "first_seen_utc": first_seen_utc,
            "last_seen_utc": last_seen_utc,
        }

        should_open = (
            total >= rule.min_requests
            and error_count >= rule.min_errors
            and error_rate > rule.error_rate_gt
        )
        should_resolve = state.open and (total == 0 or error_rate < rule.clear_rate_lt or error_count == 0)

        alerts: list[dict[str, object]] = []
        if should_open and not state.open:
            state.open = True
            state.opened_at_utc = self.now_utc()
            alerts.append(
                self._build_alert_record(
                    alert_class="route_error_rate_high",
                    transition="opened",
                    severity="warning",
                    key=key,
                    event=event,
                    starts_at_utc=state.opened_at_utc,
                    summary=(
                        f"{key.env} {key.method} {key.route} "
                        f"error rate {error_count}/{total} in {_format_duration(rule.window_seconds)}"
                    ),
                    detail=(
                        f"{key.source} {key.method} {key.route} saw {error_count} matching errors across "
                        f"{total} requests in the last {_format_duration(rule.window_seconds)}"
                    ),
                    stats=stats,
                )
            )
        elif should_resolve:
            alerts.append(
                self._build_alert_record(
                    alert_class="route_error_rate_high",
                    transition="resolved",
                    severity="info",
                    key=key,
                    event=event,
                    starts_at_utc=state.opened_at_utc or self.now_utc(),
                    summary=(
                        f"{key.env} {key.method} {key.route} "
                        f"error rate recovered to {error_count}/{total}"
                    ),
                    detail=(
                        f"{key.source} {key.method} {key.route} is back below the configured clear threshold"
                    ),
                    stats=stats,
                )
            )
            state.open = False
            state.opened_at_utc = None
        return alerts

    def _evaluate_route_seen_after_quiet(
        self,
        key: RouteAlertKey,
        event: Mapping[str, object],
        *,
        now: float,
    ) -> list[dict[str, object]]:
        rule = self.config.route_seen_after_quiet
        if not rule.enabled or not rule.matches_route(key.route):
            return []

        state = self._quiet_states.setdefault(key, _RouteQuietState())
        previous_seen_at = state.last_seen_at
        state.last_seen_at = now

        if previous_seen_at is None:
            first_seen = not state.has_seen
            state.has_seen = True
            if first_seen and not rule.emit_on_first_seen:
                return []
            quiet_seconds = rule.quiet_period_seconds
        else:
            state.has_seen = True
            quiet_seconds = now - previous_seen_at
            if quiet_seconds < float(rule.quiet_period_seconds):
                return []

        return [
            self._build_alert_record(
                alert_class="route_seen_after_quiet_period",
                transition="noticed",
                severity="info",
                key=key,
                event=event,
                starts_at_utc=_utc_from_epoch(previous_seen_at or now),
                summary=(
                    f"{key.env} {key.method} {key.route} "
                    f"seen after {_format_duration(quiet_seconds)} quiet"
                ),
                detail=(
                    f"{key.source} {key.method} {key.route} received traffic after "
                    f"{_format_duration(quiet_seconds)} without a matching event"
                ),
                stats={
                    "quiet_period_seconds": rule.quiet_period_seconds,
                    "observed_quiet_seconds": int(quiet_seconds),
                    "last_seen_before_utc": _utc_from_epoch(previous_seen_at) if previous_seen_at is not None else None,
                    "seen_at_utc": str(event.get("ts") or event.get("received_at_utc") or self.now_utc()),
                },
            )
        ]

    def _build_alert_record(
        self,
        *,
        alert_class: str,
        transition: str,
        severity: str,
        key: RouteAlertKey,
        event: Mapping[str, object],
        starts_at_utc: str,
        summary: str,
        detail: str,
        stats: Mapping[str, object],
    ) -> dict[str, object]:
        labels = dict(event.get("labels") or {})
        labels["kind"] = str(event.get("kind") or "<kind>")
        if event.get("status") is not None:
            labels["status"] = str(event["status"])
        return {
            "alert_class": alert_class,
            "transition": transition,
            "severity": severity,
            "dedupe_key": f"{alert_class}:{key.dedupe_suffix()}",
            "source": key.source,
            "env": key.env,
            "method": key.method,
            "route": key.route,
            "name": str(event.get("name") or "<name>"),
            "summary": summary,
            "detail": detail,
            "labels": labels,
            "starts_at_utc": starts_at_utc,
            "emitted_at_utc": self.now_utc(),
            "stats": dict(stats),
        }

    def _prune_error_window(self, state: _RouteErrorWindowState, *, now: float) -> None:
        cutoff = now - float(self.config.route_error_rate_high.window_seconds)
        while state.events and state.events[0][0] < cutoff:
            state.events.popleft()


def project_alerts(payload: dict[str, object], query: Mapping[str, list[str]] | None = None) -> dict[str, object]:
    resolved_query = query or {}
    limit_raw = (resolved_query.get("limit") or [""])[0].strip()
    after_raw = (resolved_query.get("after") or [""])[0].strip()

    try:
        limit = max(1, min(int(limit_raw), 200)) if limit_raw else 20
    except ValueError:
        limit = 20
    try:
        after = max(0, int(after_raw)) if after_raw else None
    except ValueError:
        after = None

    alerts = list(payload.get("alerts") or [])
    if after is not None:
        filtered = [alert for alert in alerts if int(alert.get("seq") or 0) > after]
    else:
        filtered = alerts[-limit:]

    if after is not None:
        filtered = filtered[:limit]

    oldest_seq = int(payload.get("oldest_seq") or 0)
    latest_seq = int(payload.get("latest_seq") or 0)
    truncated = bool(after is not None and oldest_seq and after < oldest_seq - 1)

    return {
        "generated_at_utc": payload.get("generated_at_utc"),
        "alerts_enabled": bool(payload.get("alerts_enabled")),
        "emitted_total": int(payload.get("emitted_total") or 0),
        "retained_total": int(payload.get("retained_total") or 0),
        "retention_seconds": int(payload.get("retention_seconds") or 0),
        "after": after,
        "oldest_seq": oldest_seq,
        "latest_seq": latest_seq,
        "truncated": truncated,
        "alerts": filtered,
    }


ALERT_PROJECTIONS = {
    "/alerts": project_alerts,
}
