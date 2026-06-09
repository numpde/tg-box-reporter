from __future__ import annotations

from datetime import datetime, timezone

from .projections import project_containers, project_problems, project_summary
from .snapshot import sort_containers


def format_bytes(value: int) -> str:
    if value < 0:
        return "n/a"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{int(value)} B"


def format_percent(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}%"


def format_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "n/a"
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or parts:
        parts.append(f"{hours}h")
    if minutes or parts:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return "".join(parts)


def _yes_no(value: object) -> str:
    return "yes" if bool(value) else "no"


def _display_env(value: object) -> str:
    text = str(value or "<env>")
    return text.capitalize() if text.islower() else text


def _describe_route_method(*, route: object, method: object) -> str:
    route_text = str(route or "").strip()
    method_text = str(method or "").strip()
    if method_text and route_text:
        return f"{method_text} {route_text}"
    if route_text:
        return route_text
    if method_text:
        return method_text
    return "<unknown route>"


def _section_lines(title: str, items: list[tuple[str, object]]) -> list[str]:
    lines = [f"{title}:"]
    for label, value in items:
        lines.append(f"- {label}: {value}")
    return lines


def _problem_summary_items(problem_summary: dict[str, object]) -> list[tuple[str, object]]:
    return [
        ("Total detected", int(problem_summary.get("total") or 0)),
        ("Critical", int(problem_summary.get("critical") or 0)),
        ("Warning", int(problem_summary.get("warning") or 0)),
        ("Info", int(problem_summary.get("info") or 0)),
    ]


def _host_health_items(host: dict[str, object]) -> list[tuple[str, object]]:
    memory = dict(host.get("memory") or {})
    swap = dict(host.get("swap") or {})
    disk = dict(host.get("disk") or {})
    return [
        ("Hostname", host.get("hostname", "<unknown>")),
        ("Uptime", format_duration(host.get("uptime_seconds"))),
        (
            "Load average",
            f"{host.get('load_1m', 0.0):.2f} / {host.get('load_5m', 0.0):.2f} / {host.get('load_15m', 0.0):.2f}",
        ),
        ("CPU count", host.get("cpu_count", 0)),
        (
            "Memory usage",
            f"{format_bytes(int(memory.get('used_bytes') or 0))} of "
            f"{format_bytes(int(memory.get('total_bytes') or 0))} "
            f"({format_percent(memory.get('used_percent'))})",
        ),
        (
            "Swap usage",
            f"{format_bytes(int(swap.get('used_bytes') or 0))} of "
            f"{format_bytes(int(swap.get('total_bytes') or 0))} "
            f"({format_percent(swap.get('used_percent'))})",
        ),
        (
            "Disk usage",
            f"{format_bytes(int(disk.get('used_bytes') or 0))} of "
            f"{format_bytes(int(disk.get('total_bytes') or 0))} "
            f"({format_percent(disk.get('used_percent'))}) on {disk.get('path', '<unknown>')}",
        ),
    ]


def _container_health_items(docker: dict[str, object]) -> list[tuple[str, object]]:
    summary = dict(docker.get("summary") or {})
    if not docker.get("available", False):
        return [
            ("Docker available", "no"),
            ("Collector detail", docker.get("error", "no data")),
        ]
    return [
        ("Docker available", "yes"),
        ("Total containers", summary.get("total", 0)),
        ("Running", summary.get("running", 0)),
        ("Restarting", summary.get("restarting", 0)),
        ("Unhealthy", summary.get("unhealthy", 0)),
        ("Exited", summary.get("exited", 0)),
    ]


def _container_sentence(container: dict[str, object]) -> str:
    status = str(container.get("status") or "unknown")
    health = str(container.get("health") or "")
    state_text = f"{status} and {health}" if health else status
    return (
        f"- {container.get('name', '<unknown>')} is {state_text}. "
        f"CPU usage is {format_percent(container.get('cpu_percent'))}, "
        f"memory usage is {format_percent(container.get('mem_percent'))}, "
        f"and restart count is {container.get('restart_count', 0)}."
    )


def _collector_error_lines(errors: list[object]) -> list[str]:
    if not errors:
        return []
    lines = ["Collector errors:"]
    for error in errors:
        source = error.get("source", "collector")
        detail = error.get("detail", "<no detail>")
        lines.append(f"- {source}: {detail}")
    return lines


def format_report(snapshot: dict[str, object], *, max_containers: int = 10) -> str:
    generated = str(snapshot.get("generated_at_utc") or "")
    host = dict(snapshot.get("host") or {})
    docker = dict(snapshot.get("docker") or {})
    containers = list(docker.get("containers") or [])
    errors = list(snapshot.get("errors") or [])

    lines = [
        f"System report for {host.get('hostname', '<unknown>')}",
        f"Generated at: {generated}",
    ]
    lines.append("")
    lines.extend(_section_lines("Host health", _host_health_items(host)))
    lines.append("")
    lines.extend(_section_lines("Container health", _container_health_items(docker)))
    if containers:
        lines.append("")
        lines.append("Top containers:")
        for container in sort_containers(containers)[:max_containers]:
            lines.append(_container_sentence(container))
        remaining = max(0, len(containers) - max_containers)
        if remaining:
            lines.append(f"- {remaining} more containers are available in the full container list.")
    collector_error_lines = _collector_error_lines(errors)
    if collector_error_lines:
        lines.append("")
        lines.extend(collector_error_lines)
    return "\n".join(lines)


def format_containers(snapshot: dict[str, object], *, max_containers: int | None = None) -> str:
    docker = dict(snapshot.get("docker") or {})
    total_containers = len(list(docker.get("containers") or []))
    limit = total_containers if max_containers is None else max_containers
    projection = project_containers(snapshot, {"limit": [str(limit)]})
    containers = list(projection.get("containers") or [])
    lines = [
        "Container status report",
        f"Generated at: {snapshot.get('generated_at_utc', '')}",
        f"Total containers visible: {projection.get('total', len(containers))}",
    ]
    if containers:
        lines.append("")
        lines.append("Visible container details:")
    for container in containers:
        lines.append(_container_sentence(container))
    remaining = max(0, int(projection.get("total", len(containers))) - len(containers))
    if remaining:
        lines.append(f"- {remaining} more containers are available in the full container list.")
    return "\n".join(lines)


def format_summary(snapshot: dict[str, object]) -> str:
    projection = project_summary(snapshot)
    generated = str(projection.get("generated_at_utc") or "")
    status = str(projection.get("status") or "unknown")
    host = dict(projection.get("host") or {})
    docker = dict(projection.get("docker") or {})
    problem_summary = dict(projection.get("problem_summary") or {})

    lines = [
        f"System summary for {host.get('hostname', '<unknown>')}",
        f"Generated at: {generated}",
        f"Overall status: {status}",
    ]
    lines.append("")
    lines.extend(_section_lines("Problem summary", _problem_summary_items(problem_summary)))
    lines.append("")
    lines.extend(_section_lines("Host health", _host_health_items(host)))
    lines.append("")
    lines.extend(_section_lines("Container health", _container_health_items(docker)))
    return "\n".join(lines)


def format_problems(snapshot: dict[str, object]) -> str:
    projection = project_problems(snapshot)
    generated = str(projection.get("generated_at_utc") or "")
    status = str(projection.get("status") or "unknown")
    problem_summary = dict(projection.get("problem_summary") or {})
    problems = list(projection.get("problems") or [])

    lines = [
        "Problems report",
        f"Generated at: {generated}",
        f"Overall status: {status}",
    ]
    lines.append("")
    lines.extend(_section_lines("Problem summary", _problem_summary_items(problem_summary)))

    if not problems:
        lines.append("")
        lines.append("Detected problems:")
        lines.append("- No problems detected.")
        return "\n".join(lines)

    lines.append("")
    lines.append("Detected problems:")
    for index, problem in enumerate(problems, start=1):
        severity = str(problem.get("severity") or "info")
        source = str(problem.get("source") or "collector")
        code = str(problem.get("code") or "unknown")
        detail = str(problem.get("detail") or "<no detail>")
        lines.append(f"Problem {index}:")
        lines.append(f"Severity: {severity}")
        lines.append(f"Source: {source}")
        lines.append(f"Code: {code}")
        lines.append(f"Detail: {detail}")
        if index != len(problems):
            lines.append("")
    return "\n".join(lines)


def _line_for_event_group(group: dict[str, object]) -> str:
    env = _display_env(group.get("env"))
    source = str(group.get("source") or "<source>")
    kind = str(group.get("kind") or "<kind>")
    name = str(group.get("name") or "<name>")
    route_method = _describe_route_method(route=group.get("route"), method=group.get("method"))
    status_text = f" with status {group['status']}" if group.get("status") is not None else ""
    return (
        f"- In {env}, {source} handled {route_method} for event {name} ({kind})"
        f"{status_text} {group.get('count', 0)} time(s). "
        f"The most recent matching event was at {group.get('last_seen_utc', '')}."
    )


def _line_for_event(event: dict[str, object]) -> str:
    route_method = _describe_route_method(route=event.get("route"), method=event.get("method"))
    bits = [
        f"- At {event.get('ts') or event.get('received_at_utc') or ''},",
        f"{_display_env(event.get('env'))}",
        str(event.get("source") or "<source>"),
        "handled",
        route_method,
        "for event",
        str(event.get("name") or "<name>"),
        f"({event.get('kind') or '<kind>'})",
    ]
    if event.get("status") is not None:
        bits.append(f"with status {event['status']}")
    if event.get("duration_ms") is not None:
        bits.append(f"and duration {event['duration_ms']} ms")
    sentence = " ".join(bits).strip() + "."
    extra_lines: list[str] = [sentence]
    if event.get("detail"):
        extra_lines.append(f"  Detail: {event['detail']}")
    labels = dict(event.get("labels") or {})
    if labels:
        label_text = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
        extra_lines.append(f"  Labels: {label_text}")
    return "\n".join(extra_lines)


def format_events(payload: dict[str, object]) -> str:
    generated = str(payload.get("generated_at_utc") or "")
    summary = list(payload.get("summary") or [])
    recent = list(payload.get("recent") or [])
    lines = [
        "Recent application events",
        f"Generated at: {generated}",
        f"Event ingestion enabled: {_yes_no(payload.get('ingest_enabled'))}",
        "",
    ]
    lines.extend(
        _section_lines(
            "Event retention",
            [
                ("Received total", int(payload.get("received_total") or 0)),
                ("Retained events", int(payload.get("retained_total") or 0)),
                ("Retention window", format_duration(payload.get("retention_seconds"))),
            ],
        )
    )

    lines.append("")
    if summary:
        lines.append("Grouped activity:")
        for group in summary:
            lines.append(_line_for_event_group(group))
    else:
        lines.append("Grouped activity:")
        lines.append("- No recent event groups.")

    lines.append("")
    if recent:
        lines.append("Most recent raw events:")
        for event in recent:
            lines.append(_line_for_event(event))
    else:
        lines.append("Most recent raw events:")
        lines.append("- No recent events.")
    return "\n".join(lines)


def _alert_heading(alert: dict[str, object]) -> str:
    alert_class = str(alert.get("alert_class") or "<class>")
    env = _display_env(alert.get("env"))
    transition = str(alert.get("transition") or "noticed")
    stats = dict(alert.get("stats") or {})
    if alert_class == "route_error_rate_high":
        if transition == "resolved":
            return f"Alert resolved: Server error rate recovered on {env}"
        return f"Alert: Elevated server error rate on {env}"
    if alert_class == "route_seen_after_quiet_period":
        if stats.get("startup_cold_start"):
            return f"Alert: {env} route first seen after collector start"
        return f"Alert: {env} route seen after quiet period"
    if alert_class == "synthetic_check_failed":
        if transition == "resolved":
            return f"Alert resolved: Synthetic check recovered on {env}"
        return f"Alert: Synthetic check failed on {env}"
    if alert_class == "synthetic_check_succeeded":
        return f"Alert: Synthetic check succeeded on {env}"
    return f"Alert: {alert_class} on {env}"


def _humanize_alert_stat(key: str, value: object) -> tuple[str, str]:
    if key in {"window_seconds", "quiet_period_seconds", "observed_quiet_seconds"}:
        return (
            {
                "window_seconds": "Window length",
                "quiet_period_seconds": "Configured quiet period",
                "observed_quiet_seconds": "Observed quiet period",
            }[key],
            "n/a" if value is None else format_duration(value),
        )
    if key == "error_rate":
        return ("Error rate", str(value))
    if key == "latest_status":
        return ("Latest HTTP status", str(value))
    if key == "duration_ms":
        return ("Duration", f"{value} ms")
    if key == "target":
        return ("Synthetic target", str(value))
    if key == "result":
        return ("Synthetic result", str(value))
    if key == "total_requests":
        return ("Total requests in window", str(value))
    if key == "error_requests":
        return ("Error requests in window", str(value))
    if key == "first_seen_utc":
        return ("First seen in current window", str(value))
    if key == "last_seen_utc":
        return ("Last seen in current window", str(value))
    if key == "last_seen_before_utc":
        return ("Last seen before this event", str(value))
    if key == "seen_at_utc":
        return ("Seen at", str(value))
    if key == "startup_cold_start":
        return ("Collector cold start", _yes_no(value))
    if key == "status_classes":
        return ("Matching status classes", ", ".join(str(item) for item in value))
    return (key.replace("_", " ").capitalize(), str(value))


def format_alert_record(alert: dict[str, object]) -> str:
    route_text = _describe_route_method(route=alert.get("route"), method=alert.get("method"))
    labels = dict(alert.get("labels") or {})
    stats = dict(alert.get("stats") or {})
    alert_class = str(alert.get("alert_class") or "")
    target = str(alert.get("target") or labels.get("target") or "n/a")
    is_synthetic_check = alert_class in {"synthetic_check_failed", "synthetic_check_succeeded"}
    subject_line = f"Synthetic target: {target}" if is_synthetic_check else f"Route: {route_text}"
    lines = [
        _alert_heading(alert),
        f"Severity: {alert.get('severity', 'info')}",
        subject_line,
        f"Environment: {alert.get('env', '<env>')}",
        f"Source service: {alert.get('source', '<source>')}",
        "",
        "What happened:",
        str(alert.get("summary") or "<no summary>"),
    ]
    detail = str(alert.get("detail") or "").strip()
    if detail:
        lines.append("")
        lines.append("Details:")
        lines.append(detail)
    lines.append("")
    lines.extend(
        _section_lines(
            "Alert metadata",
            [
                ("Alert type", alert.get("alert_class", "<class>")),
                ("Transition", alert.get("transition", "noticed")),
                ("Sequence number", int(alert.get("seq") or 0)),
                ("Event name", alert.get("name", "<name>")),
                ("HTTP status", labels.get("status", "n/a")),
                ("Event kind", labels.get("kind", "n/a")),
                ("Alert emitted at", alert.get("emitted_at_utc", "n/a")),
            ],
        )
    )
    if stats:
        lines.append("")
        lines.append("Alert stats:")
        for key, value in sorted(stats.items()):
            label, rendered = _humanize_alert_stat(key, value)
            lines.append(f"- {label}: {rendered}")
    return "\n".join(lines)


def format_alerts(payload: dict[str, object]) -> str:
    generated = str(payload.get("generated_at_utc") or "")
    alerts = list(payload.get("alerts") or [])
    lines = [
        "Alert feed",
        f"Generated at: {generated}",
        f"Alerts enabled: {_yes_no(payload.get('alerts_enabled'))}",
    ]
    lines.append("")
    lines.extend(
        _section_lines(
            "Feed state",
            [
                ("Total alerts emitted", int(payload.get("emitted_total") or 0)),
                ("Retained alerts", int(payload.get("retained_total") or 0)),
                ("Retention window", format_duration(payload.get("retention_seconds"))),
                ("Oldest sequence", int(payload.get("oldest_seq") or 0)),
                ("Latest sequence", int(payload.get("latest_seq") or 0)),
                ("Requested after sequence", payload.get("after") if payload.get("after") is not None else "-"),
                ("Feed truncated", _yes_no(payload.get("truncated"))),
            ],
        )
    )
    if not alerts:
        lines.append("")
        lines.append("Retained alerts:")
        lines.append("- No retained alerts.")
        return "\n".join(lines)
    lines.append("")
    lines.append("Retained alerts:")
    for index, alert in enumerate(alerts):
        if index:
            lines.append("")
        lines.append(format_alert_record(alert))
    return "\n".join(lines)


def split_message(text: str, *, limit: int = 3900) -> list[str]:
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        line_size = len(line) + 1
        if current and size + line_size > limit:
            parts.append("\n".join(current))
            current = [line]
            size = line_size
            continue
        current.append(line)
        size += line_size
    if current:
        parts.append("\n".join(current))
    return parts


def utc_timestamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
