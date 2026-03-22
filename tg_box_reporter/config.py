from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Pattern

from .alerts import CollectorAlertsConfig, RouteErrorRateHighConfig, RouteSeenAfterQuietConfig


class ConfigError(ValueError):
    pass


DEFAULT_COLLECTOR_ALERT_LOAD_PER_CPU_GT = 1.5
DEFAULT_COLLECTOR_ALERT_MEM_PERCENT_GT = 90.0
DEFAULT_COLLECTOR_ALERT_SWAP_USED_MB_GT = 256.0
DEFAULT_COLLECTOR_ALERT_DISK_PERCENT_GT = 90.0
DEFAULT_COLLECTOR_ALERT_CONTAINER_RESTART_COUNT_GT = 5
DEFAULT_COLLECTOR_ALERT_CONTAINER_CPU_PERCENT_GT = 85.0
DEFAULT_COLLECTOR_ALERT_CONTAINER_MEM_PERCENT_GT = 85.0
DEFAULT_COLLECTOR_DOCKER_TIMEOUT_SECONDS = 10.0
DEFAULT_COLLECTOR_EVENT_MAX_RECENT = 200
DEFAULT_COLLECTOR_EVENT_RETENTION_SECONDS = 3600
DEFAULT_COLLECTOR_EVENT_MAX_BYTES = 16384
DEFAULT_COLLECTOR_ALERT_MAX_RECENT = 200
DEFAULT_COLLECTOR_ALERT_RETENTION_SECONDS = 86400
DEFAULT_COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_WINDOW_SECONDS = 300
DEFAULT_COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_MIN_REQUESTS = 10
DEFAULT_COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_MIN_ERRORS = 5
DEFAULT_COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_ERROR_RATE_GT = 0.5
DEFAULT_COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_CLEAR_RATE_LT = 0.2
DEFAULT_COLLECTOR_ALERT_ROUTE_SEEN_AFTER_QUIET_PERIOD_SECONDS = 21600
DEFAULT_RELAY_HEARTBEAT_PATH = "/tmp/tg-box-reporter-relay.heartbeat"
DEFAULT_RELAY_HEALTH_STALE_SECONDS = 120
DEFAULT_RELAY_HEALTH_SAFETY_MARGIN_SECONDS = 10
DEFAULT_RELAY_ALERT_POLL_SECONDS = 15
DEFAULT_RELAY_ALERT_BATCH_SIZE = 20


def _optional(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else default


def _required(name: str) -> str:
    value = _optional(name)
    if not value:
        raise ConfigError(f"{name} must be set")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be one of 1/0/true/false/yes/no/on/off")


def _int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = int(raw.strip())
        except ValueError as exc:
            raise ConfigError(f"{name} must be an integer") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def _float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = float(raw.strip())
        except ValueError as exc:
            raise ConfigError(f"{name} must be a number") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def _csv(name: str) -> tuple[str, ...]:
    raw = _optional(name)
    if not raw:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _csv_lower(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _optional(name)
    if not raw:
        return default
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


def _regex(name: str) -> Pattern[str] | None:
    raw = _optional(name)
    if not raw:
        return None
    try:
        return re.compile(raw)
    except re.error as exc:
        raise ConfigError(f"{name} must be a valid regular expression") from exc


@dataclass(frozen=True)
class CollectorConfig:
    bind_host: str
    port: int
    cache_seconds: int
    host_proc: str
    host_root: str
    disk_path: str
    docker_bin: str
    include_stopped: bool
    name_include_regex: str
    name_exclude_regex: str
    require_docker: bool
    alert_load_per_cpu_gt: float = DEFAULT_COLLECTOR_ALERT_LOAD_PER_CPU_GT
    alert_mem_percent_gt: float = DEFAULT_COLLECTOR_ALERT_MEM_PERCENT_GT
    alert_swap_used_mb_gt: float = DEFAULT_COLLECTOR_ALERT_SWAP_USED_MB_GT
    alert_disk_percent_gt: float = DEFAULT_COLLECTOR_ALERT_DISK_PERCENT_GT
    alert_container_restart_count_gt: int = DEFAULT_COLLECTOR_ALERT_CONTAINER_RESTART_COUNT_GT
    alert_container_cpu_percent_gt: float = DEFAULT_COLLECTOR_ALERT_CONTAINER_CPU_PERCENT_GT
    alert_container_mem_percent_gt: float = DEFAULT_COLLECTOR_ALERT_CONTAINER_MEM_PERCENT_GT
    docker_command_timeout_seconds: float = DEFAULT_COLLECTOR_DOCKER_TIMEOUT_SECONDS
    event_token: str = ""
    event_max_recent: int = DEFAULT_COLLECTOR_EVENT_MAX_RECENT
    event_retention_seconds: int = DEFAULT_COLLECTOR_EVENT_RETENTION_SECONDS
    event_max_bytes: int = DEFAULT_COLLECTOR_EVENT_MAX_BYTES
    alerts: CollectorAlertsConfig = CollectorAlertsConfig()

    @classmethod
    def from_env(cls) -> "CollectorConfig":
        host_root = _optional("COLLECTOR_HOST_ROOT", "/") or "/"
        disk_path = _optional("COLLECTOR_DISK_PATH", host_root) or host_root
        route_error_status_classes = _csv_lower(
            "COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_STATUS_CLASSES",
            ("5xx",),
        )
        if not route_error_status_classes:
            raise ConfigError("COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_STATUS_CLASSES must include at least one class")
        valid_status_classes = {"2xx", "3xx", "4xx", "5xx"}
        invalid_status_classes = [value for value in route_error_status_classes if value not in valid_status_classes]
        if invalid_status_classes:
            raise ConfigError(
                "COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_STATUS_CLASSES must only include "
                f"{', '.join(sorted(valid_status_classes))}"
            )
        route_error_rate_high = RouteErrorRateHighConfig(
            enabled=_bool("COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_ENABLED", False),
            allowlist_regex=_regex("COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_ALLOWLIST_REGEX"),
            window_seconds=_int(
                "COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_WINDOW_SECONDS",
                DEFAULT_COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_WINDOW_SECONDS,
                minimum=1,
            ),
            min_requests=_int(
                "COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_MIN_REQUESTS",
                DEFAULT_COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_MIN_REQUESTS,
                minimum=1,
            ),
            min_errors=_int(
                "COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_MIN_ERRORS",
                DEFAULT_COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_MIN_ERRORS,
                minimum=1,
            ),
            error_rate_gt=_float(
                "COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_ERROR_RATE_GT",
                DEFAULT_COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_ERROR_RATE_GT,
                minimum=0.0,
            ),
            clear_rate_lt=_float(
                "COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_CLEAR_RATE_LT",
                DEFAULT_COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_CLEAR_RATE_LT,
                minimum=0.0,
            ),
            status_classes=route_error_status_classes,
        )
        if route_error_rate_high.clear_rate_lt >= route_error_rate_high.error_rate_gt:
            raise ConfigError(
                "COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_CLEAR_RATE_LT must be < "
                "COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_ERROR_RATE_GT"
            )
        if route_error_rate_high.min_errors > route_error_rate_high.min_requests:
            raise ConfigError(
                "COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_MIN_ERRORS must be <= "
                "COLLECTOR_ALERT_ROUTE_ERROR_RATE_HIGH_MIN_REQUESTS"
            )

        route_seen_after_quiet = RouteSeenAfterQuietConfig(
            enabled=_bool("COLLECTOR_ALERT_ROUTE_SEEN_AFTER_QUIET_ENABLED", False),
            allowlist_regex=_regex("COLLECTOR_ALERT_ROUTE_SEEN_AFTER_QUIET_ALLOWLIST_REGEX"),
            quiet_period_seconds=_int(
                "COLLECTOR_ALERT_ROUTE_SEEN_AFTER_QUIET_PERIOD_SECONDS",
                DEFAULT_COLLECTOR_ALERT_ROUTE_SEEN_AFTER_QUIET_PERIOD_SECONDS,
                minimum=1,
            ),
            emit_on_first_seen=_bool("COLLECTOR_ALERT_ROUTE_SEEN_AFTER_QUIET_EMIT_ON_FIRST_SEEN", False),
        )

        return cls(
            bind_host=_optional("COLLECTOR_BIND_HOST", "127.0.0.1") or "127.0.0.1",
            port=_int("COLLECTOR_PORT", 9707, minimum=1),
            cache_seconds=_int("COLLECTOR_CACHE_SECONDS", 5, minimum=0),
            host_proc=_optional("COLLECTOR_HOST_PROC", "/proc") or "/proc",
            host_root=host_root,
            disk_path=disk_path,
            docker_bin=_optional("COLLECTOR_DOCKER_BIN", "docker") or "docker",
            include_stopped=_bool("COLLECTOR_INCLUDE_STOPPED", True),
            name_include_regex=_optional("COLLECTOR_NAME_INCLUDE_REGEX"),
            name_exclude_regex=_optional("COLLECTOR_NAME_EXCLUDE_REGEX"),
            require_docker=_bool("COLLECTOR_REQUIRE_DOCKER", False),
            alert_load_per_cpu_gt=_float(
                "COLLECTOR_ALERT_LOAD_PER_CPU_GT",
                DEFAULT_COLLECTOR_ALERT_LOAD_PER_CPU_GT,
                minimum=-1.0,
            ),
            alert_mem_percent_gt=_float(
                "COLLECTOR_ALERT_MEM_PERCENT_GT",
                DEFAULT_COLLECTOR_ALERT_MEM_PERCENT_GT,
                minimum=-1.0,
            ),
            alert_swap_used_mb_gt=_float(
                "COLLECTOR_ALERT_SWAP_USED_MB_GT",
                DEFAULT_COLLECTOR_ALERT_SWAP_USED_MB_GT,
                minimum=-1.0,
            ),
            alert_disk_percent_gt=_float(
                "COLLECTOR_ALERT_DISK_PERCENT_GT",
                DEFAULT_COLLECTOR_ALERT_DISK_PERCENT_GT,
                minimum=-1.0,
            ),
            alert_container_restart_count_gt=_int(
                "COLLECTOR_ALERT_CONTAINER_RESTART_COUNT_GT",
                DEFAULT_COLLECTOR_ALERT_CONTAINER_RESTART_COUNT_GT,
                minimum=-1,
            ),
            alert_container_cpu_percent_gt=_float(
                "COLLECTOR_ALERT_CONTAINER_CPU_PERCENT_GT",
                DEFAULT_COLLECTOR_ALERT_CONTAINER_CPU_PERCENT_GT,
                minimum=-1.0,
            ),
            alert_container_mem_percent_gt=_float(
                "COLLECTOR_ALERT_CONTAINER_MEM_PERCENT_GT",
                DEFAULT_COLLECTOR_ALERT_CONTAINER_MEM_PERCENT_GT,
                minimum=-1.0,
            ),
            docker_command_timeout_seconds=_float(
                "COLLECTOR_DOCKER_TIMEOUT_SECONDS",
                DEFAULT_COLLECTOR_DOCKER_TIMEOUT_SECONDS,
                minimum=1.0,
            ),
            event_token=_optional("COLLECTOR_EVENT_TOKEN"),
            event_max_recent=_int(
                "COLLECTOR_EVENT_MAX_RECENT",
                DEFAULT_COLLECTOR_EVENT_MAX_RECENT,
                minimum=1,
            ),
            event_retention_seconds=_int(
                "COLLECTOR_EVENT_RETENTION_SECONDS",
                DEFAULT_COLLECTOR_EVENT_RETENTION_SECONDS,
                minimum=1,
            ),
            event_max_bytes=_int(
                "COLLECTOR_EVENT_MAX_BYTES",
                DEFAULT_COLLECTOR_EVENT_MAX_BYTES,
                minimum=128,
            ),
            alerts=CollectorAlertsConfig(
                enabled=_bool("COLLECTOR_ALERTS_ENABLED", False),
                max_recent=_int(
                    "COLLECTOR_ALERT_MAX_RECENT",
                    DEFAULT_COLLECTOR_ALERT_MAX_RECENT,
                    minimum=1,
                ),
                retention_seconds=_int(
                    "COLLECTOR_ALERT_RETENTION_SECONDS",
                    DEFAULT_COLLECTOR_ALERT_RETENTION_SECONDS,
                    minimum=1,
                ),
                route_error_rate_high=route_error_rate_high,
                route_seen_after_quiet=route_seen_after_quiet,
            ),
        )


@dataclass(frozen=True)
class RelayConfig:
    bot_token: str
    collector_url: str
    mode: str
    chat_id: str
    allowed_chat_ids: tuple[str, ...]
    interval_seconds: int
    startup_report: bool
    request_timeout_seconds: int
    get_updates_timeout_seconds: int
    max_containers: int
    telegram_api_base: str
    heartbeat_path: str = DEFAULT_RELAY_HEARTBEAT_PATH
    health_stale_seconds: int = DEFAULT_RELAY_HEALTH_STALE_SECONDS
    alerts_enabled: bool = False
    alert_poll_seconds: int = DEFAULT_RELAY_ALERT_POLL_SECONDS
    alert_batch_size: int = DEFAULT_RELAY_ALERT_BATCH_SIZE

    @classmethod
    def from_env(cls) -> "RelayConfig":
        mode = _optional("RELAY_MODE", "hybrid") or "hybrid"
        if mode not in {"scheduled", "polling", "hybrid"}:
            raise ConfigError("RELAY_MODE must be one of scheduled, polling, hybrid")
        alerts_enabled = _bool("RELAY_ALERTS_ENABLED", False)

        chat_id = _optional("TG_CHAT_ID")
        if (mode in {"scheduled", "hybrid"} or alerts_enabled) and not chat_id:
            raise ConfigError("TG_CHAT_ID must be set when RELAY_MODE includes scheduled delivery or alert delivery")

        allowed = _csv("TG_ALLOWED_CHAT_IDS")
        if not allowed and chat_id:
            allowed = (chat_id,)

        request_timeout_seconds = _int("RELAY_REQUEST_TIMEOUT_SECONDS", 20, minimum=1)
        get_updates_timeout_seconds = _int("TG_GET_UPDATES_TIMEOUT_SECONDS", 30, minimum=1)
        health_stale_seconds = _int(
            "RELAY_HEALTH_STALE_SECONDS",
            DEFAULT_RELAY_HEALTH_STALE_SECONDS,
            minimum=1,
        )
        minimum_health_stale_seconds = max(
            request_timeout_seconds,
            get_updates_timeout_seconds + 5,
        ) + DEFAULT_RELAY_HEALTH_SAFETY_MARGIN_SECONDS
        if health_stale_seconds < minimum_health_stale_seconds:
            raise ConfigError(
                "RELAY_HEALTH_STALE_SECONDS must be >= "
                f"{minimum_health_stale_seconds} for the current request and long-poll timeouts"
            )

        return cls(
            bot_token=_required("TG_BOT_TOKEN"),
            collector_url=_optional("COLLECTOR_URL", "http://127.0.0.1:9707/snapshot")
            or "http://127.0.0.1:9707/snapshot",
            mode=mode,
            chat_id=chat_id,
            allowed_chat_ids=allowed,
            interval_seconds=_int("RELAY_INTERVAL_SECONDS", 900, minimum=1),
            startup_report=_bool("RELAY_STARTUP_REPORT", False),
            request_timeout_seconds=request_timeout_seconds,
            get_updates_timeout_seconds=get_updates_timeout_seconds,
            max_containers=_int("REPORT_MAX_CONTAINERS", 10, minimum=1),
            telegram_api_base=_optional("TG_API_BASE", "https://api.telegram.org") or "https://api.telegram.org",
            heartbeat_path=_optional("RELAY_HEARTBEAT_PATH", DEFAULT_RELAY_HEARTBEAT_PATH)
            or DEFAULT_RELAY_HEARTBEAT_PATH,
            health_stale_seconds=health_stale_seconds,
            alerts_enabled=alerts_enabled,
            alert_poll_seconds=_int("RELAY_ALERT_POLL_SECONDS", DEFAULT_RELAY_ALERT_POLL_SECONDS, minimum=1),
            alert_batch_size=_int("RELAY_ALERT_BATCH_SIZE", DEFAULT_RELAY_ALERT_BATCH_SIZE, minimum=1),
        )
