from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable

from .projections import QueryParams, parse_limit

MAX_EVENT_LABELS = 20
MAX_EVENT_FIELD_LENGTH = 120
MAX_EVENT_DETAIL_LENGTH = 300

EventProjectionBuilder = Callable[[dict[str, object], QueryParams], dict[str, object]]


class EventValidationError(ValueError):
    pass


def _utc_from_epoch(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now() -> str:
    return _utc_from_epoch(time.time())


def _normalize_string(value: object, *, field: str, maximum: int = MAX_EVENT_FIELD_LENGTH) -> str:
    if not isinstance(value, str):
        raise EventValidationError(f"{field} must be a non-empty string")
    normalized = value.strip()
    if not normalized:
        raise EventValidationError(f"{field} must be a non-empty string")
    if len(normalized) > maximum:
        raise EventValidationError(f"{field} must be <= {maximum} characters")
    return normalized


def _normalize_optional_string(
    value: object,
    *,
    field: str,
    maximum: int = MAX_EVENT_FIELD_LENGTH,
) -> str | None:
    if value is None:
        return None
    return _normalize_string(value, field=field, maximum=maximum)


def _normalize_timestamp(value: object, *, field: str) -> str:
    normalized = _normalize_string(value, field=field)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EventValidationError(f"{field} must be ISO-8601 compatible") from exc
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_int(value: object, *, field: str, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EventValidationError(f"{field} must be an integer")
    if value < minimum:
        raise EventValidationError(f"{field} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise EventValidationError(f"{field} must be <= {maximum}")
    return value


def _normalize_float(value: object, *, field: str, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EventValidationError(f"{field} must be a number")
    normalized = float(value)
    if normalized < minimum:
        raise EventValidationError(f"{field} must be >= {minimum}")
    return normalized


def _normalize_labels(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise EventValidationError("labels must be an object")
    if len(value) > MAX_EVENT_LABELS:
        raise EventValidationError(f"labels must include at most {MAX_EVENT_LABELS} keys")
    normalized: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = _normalize_string(raw_key, field="labels key", maximum=64)
        if raw_value is None:
            continue
        if isinstance(raw_value, (dict, list, tuple, set)):
            raise EventValidationError("labels values must be scalars")
        rendered = str(raw_value).strip()
        if not rendered:
            continue
        if len(rendered) > MAX_EVENT_FIELD_LENGTH:
            raise EventValidationError(f"labels[{key}] must be <= {MAX_EVENT_FIELD_LENGTH} characters")
        normalized[key] = rendered
    return normalized


def normalize_event(payload: dict[str, object], *, now_utc: Callable[[], str] = _utc_now) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise EventValidationError("event payload must be a JSON object")

    event = {
        "source": _normalize_string(payload.get("source"), field="source"),
        "env": _normalize_string(payload.get("env"), field="env"),
        "kind": _normalize_string(payload.get("kind"), field="kind"),
        "name": _normalize_string(payload.get("name"), field="name"),
        "ts": _normalize_timestamp(payload["ts"], field="ts") if "ts" in payload else now_utc(),
        "received_at_utc": now_utc(),
    }

    route = _normalize_optional_string(payload.get("route"), field="route", maximum=256)
    method = _normalize_optional_string(payload.get("method"), field="method", maximum=16)
    detail = _normalize_optional_string(payload.get("detail"), field="detail", maximum=MAX_EVENT_DETAIL_LENGTH)
    if route is not None:
        event["route"] = route
    if method is not None:
        event["method"] = method.upper()
    if detail is not None:
        event["detail"] = detail
    if "status" in payload and payload["status"] is not None:
        event["status"] = _normalize_int(payload["status"], field="status", minimum=100, maximum=599)
    if "duration_ms" in payload and payload["duration_ms"] is not None:
        event["duration_ms"] = round(_normalize_float(payload["duration_ms"], field="duration_ms"), 3)

    labels = _normalize_labels(payload.get("labels"))
    if labels:
        event["labels"] = labels
    return event


def _event_group_key(event: dict[str, object]) -> tuple[object, ...]:
    return (
        event.get("source"),
        event.get("env"),
        event.get("kind"),
        event.get("name"),
        event.get("route"),
        event.get("method"),
        event.get("status"),
    )


def _group_from_event(event: dict[str, object]) -> dict[str, object]:
    group = {
        "source": event.get("source"),
        "env": event.get("env"),
        "kind": event.get("kind"),
        "name": event.get("name"),
        "count": 0,
        "last_seen_utc": event.get("ts"),
    }
    for field in ("route", "method", "status"):
        if field in event:
            group[field] = event[field]
    return group


class EventStore:
    def __init__(
        self,
        *,
        max_recent: int,
        retention_seconds: int,
        clock: Callable[[], float] = time.time,
        now_utc: Callable[[], str] = _utc_now,
    ):
        self.max_recent = max_recent
        self.retention_seconds = retention_seconds
        self.clock = clock
        self.now_utc = now_utc
        self._lock = threading.Lock()
        self._events: deque[tuple[float, dict[str, object]]] = deque()
        self._received_total = 0

    def ingest(self, payload: dict[str, object]) -> dict[str, object]:
        event = normalize_event(payload, now_utc=self.now_utc)
        now = self.clock()
        with self._lock:
            self._prune_locked(now)
            self._events.append((now, event))
            self._received_total += 1
            self._prune_locked(now)
        return dict(event)

    def snapshot(self) -> dict[str, object]:
        now = self.clock()
        with self._lock:
            self._prune_locked(now)
            events = [dict(event) for _, event in reversed(self._events)]
            received_total = self._received_total
        return {
            "generated_at_utc": self.now_utc(),
            "received_total": received_total,
            "retained_total": len(events),
            "retention_seconds": self.retention_seconds,
            "recent": events,
            "summary": self._build_summary(events),
        }

    def _build_summary(self, events: list[dict[str, object]]) -> list[dict[str, object]]:
        groups: dict[tuple[object, ...], dict[str, object]] = {}
        for event in events:
            key = _event_group_key(event)
            group = groups.get(key)
            if group is None:
                group = _group_from_event(event)
                groups[key] = group
            group["count"] = int(group.get("count") or 0) + 1
            last_seen = str(group.get("last_seen_utc") or "")
            current_seen = str(event.get("ts") or "")
            if current_seen > last_seen:
                group["last_seen_utc"] = current_seen
        return sorted(
            groups.values(),
            key=lambda item: (-int(item.get("count") or 0), str(item.get("last_seen_utc") or "")),
        )

    def _prune_locked(self, now: float) -> None:
        cutoff = now - float(self.retention_seconds)
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()
        while len(self._events) > self.max_recent:
            self._events.popleft()


def _event_projection_base(payload: dict[str, object]) -> dict[str, object]:
    return {
        "generated_at_utc": payload.get("generated_at_utc"),
        "ingest_enabled": payload.get("ingest_enabled", False),
        "received_total": payload.get("received_total", 0),
        "retained_total": payload.get("retained_total", 0),
        "retention_seconds": payload.get("retention_seconds", 0),
    }


def project_events(payload: dict[str, object], query: QueryParams | None = None) -> dict[str, object]:
    resolved_query = query or {}
    recent_limit = parse_limit(resolved_query, default=10, maximum=200)
    summary_limit = parse_limit(resolved_query, key="groups", default=10, maximum=100)
    projection = _event_projection_base(payload)
    projection["summary"] = list(payload.get("summary") or [])[:summary_limit]
    projection["recent"] = list(payload.get("recent") or [])[:recent_limit]
    return projection


def project_events_recent(payload: dict[str, object], query: QueryParams | None = None) -> dict[str, object]:
    resolved_query = query or {}
    limit = parse_limit(resolved_query, default=20, maximum=200)
    projection = _event_projection_base(payload)
    projection["events"] = list(payload.get("recent") or [])[:limit]
    return projection


def project_events_summary(payload: dict[str, object], query: QueryParams | None = None) -> dict[str, object]:
    resolved_query = query or {}
    limit = parse_limit(resolved_query, key="groups", default=20, maximum=100)
    projection = _event_projection_base(payload)
    projection["groups"] = list(payload.get("summary") or [])[:limit]
    return projection


EVENT_PROJECTIONS: dict[str, EventProjectionBuilder] = {
    "/events": project_events,
    "/events/recent": project_events_recent,
    "/events/summary": project_events_summary,
}
