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


def _line_for_container(container: dict[str, object]) -> str:
    status = str(container.get("status") or "unknown")
    health = str(container.get("health") or "")
    status_bits = [status]
    if health:
        status_bits.append(health)
    return (
        f"- {container.get('name', '<unknown>')} "
        f"cpu={format_percent(container.get('cpu_percent'))} "
        f"mem={format_percent(container.get('mem_percent'))} "
        f"restarts={container.get('restart_count', 0)} "
        f"state={'/'.join(status_bits)}"
    )


def _problem_summary_line(problem_summary: dict[str, object]) -> str:
    return (
        "problems "
        f"total={int(problem_summary.get('total') or 0)} "
        f"critical={int(problem_summary.get('critical') or 0)} "
        f"warning={int(problem_summary.get('warning') or 0)} "
        f"info={int(problem_summary.get('info') or 0)}"
    )


def format_report(snapshot: dict[str, object], *, max_containers: int = 10) -> str:
    generated = str(snapshot.get("generated_at_utc") or "")
    host = dict(snapshot.get("host") or {})
    docker = dict(snapshot.get("docker") or {})
    memory = dict(host.get("memory") or {})
    swap = dict(host.get("swap") or {})
    disk = dict(host.get("disk") or {})
    summary = dict(docker.get("summary") or {})
    containers = list(docker.get("containers") or [])
    errors = list(snapshot.get("errors") or [])

    lines = [
        f"host {host.get('hostname', '<unknown>')} snapshot {generated}",
        (
            f"uptime {format_duration(host.get('uptime_seconds'))}  "
            f"load {host.get('load_1m', 0.0):.2f}/{host.get('load_5m', 0.0):.2f}/{host.get('load_15m', 0.0):.2f}  "
            f"cpus {host.get('cpu_count', 0)}"
        ),
        (
            f"mem {format_bytes(int(memory.get('used_bytes') or 0))}/"
            f"{format_bytes(int(memory.get('total_bytes') or 0))} "
            f"({format_percent(memory.get('used_percent'))})"
        ),
        (
            f"swap {format_bytes(int(swap.get('used_bytes') or 0))}/"
            f"{format_bytes(int(swap.get('total_bytes') or 0))} "
            f"({format_percent(swap.get('used_percent'))})"
        ),
        (
            f"disk {format_bytes(int(disk.get('used_bytes') or 0))}/"
            f"{format_bytes(int(disk.get('total_bytes') or 0))} "
            f"({format_percent(disk.get('used_percent'))}) path={disk.get('path', '<unknown>')}"
        ),
    ]

    if docker.get("available", False):
        lines.append(
            "containers "
            f"total={summary.get('total', 0)} "
            f"running={summary.get('running', 0)} "
            f"restarting={summary.get('restarting', 0)} "
            f"unhealthy={summary.get('unhealthy', 0)} "
            f"exited={summary.get('exited', 0)}"
        )
        if containers:
            lines.append("")
            lines.append("top containers:")
            for container in sort_containers(containers)[:max_containers]:
                lines.append(_line_for_container(container))
            remaining = max(0, len(containers) - max_containers)
            if remaining:
                lines.append(f"... {remaining} more containers")
    else:
        lines.append(f"docker unavailable: {docker.get('error', 'no data')}")

    if errors:
        lines.append("")
        lines.append("collector errors:")
        for error in errors:
            source = error.get("source", "collector")
            detail = error.get("detail", "<no detail>")
            lines.append(f"- {source}: {detail}")

    return "\n".join(lines)


def format_containers(snapshot: dict[str, object], *, max_containers: int | None = None) -> str:
    docker = dict(snapshot.get("docker") or {})
    total_containers = len(list(docker.get("containers") or []))
    limit = total_containers if max_containers is None else max_containers
    projection = project_containers(snapshot, {"limit": [str(limit)]})
    containers = list(projection.get("containers") or [])
    lines = [
        f"container list generated {snapshot.get('generated_at_utc', '')}",
        f"total containers {projection.get('total', len(containers))}",
    ]
    for container in containers:
        lines.append(_line_for_container(container))
    remaining = max(0, int(projection.get("total", len(containers))) - len(containers))
    if remaining:
        lines.append(f"... {remaining} more containers")
    return "\n".join(lines)


def format_summary(snapshot: dict[str, object]) -> str:
    projection = project_summary(snapshot)
    generated = str(projection.get("generated_at_utc") or "")
    status = str(projection.get("status") or "unknown")
    host = dict(projection.get("host") or {})
    docker = dict(projection.get("docker") or {})
    memory = dict(host.get("memory") or {})
    swap = dict(host.get("swap") or {})
    disk = dict(host.get("disk") or {})
    summary = dict(docker.get("summary") or {})
    problem_summary = dict(projection.get("problem_summary") or {})

    lines = [
        f"summary generated {generated}",
        f"status {status}",
        _problem_summary_line(problem_summary),
        (
            f"host {host.get('hostname', '<unknown>')}  "
            f"uptime {format_duration(host.get('uptime_seconds'))}  "
            f"load {host.get('load_1m', 0.0):.2f}/{host.get('load_5m', 0.0):.2f}/{host.get('load_15m', 0.0):.2f}  "
            f"cpus {host.get('cpu_count', 0)}"
        ),
        (
            f"mem {format_bytes(int(memory.get('used_bytes') or 0))}/"
            f"{format_bytes(int(memory.get('total_bytes') or 0))} "
            f"({format_percent(memory.get('used_percent'))})"
        ),
        (
            f"swap {format_bytes(int(swap.get('used_bytes') or 0))}/"
            f"{format_bytes(int(swap.get('total_bytes') or 0))} "
            f"({format_percent(swap.get('used_percent'))})"
        ),
        (
            f"disk {format_bytes(int(disk.get('used_bytes') or 0))}/"
            f"{format_bytes(int(disk.get('total_bytes') or 0))} "
            f"({format_percent(disk.get('used_percent'))}) path={disk.get('path', '<unknown>')}"
        ),
    ]

    if docker.get("available", False):
        lines.append(
            "containers "
            f"total={summary.get('total', 0)} "
            f"running={summary.get('running', 0)} "
            f"restarting={summary.get('restarting', 0)} "
            f"unhealthy={summary.get('unhealthy', 0)} "
            f"exited={summary.get('exited', 0)}"
        )
    else:
        lines.append("docker unavailable")

    return "\n".join(lines)


def format_problems(snapshot: dict[str, object]) -> str:
    projection = project_problems(snapshot)
    generated = str(projection.get("generated_at_utc") or "")
    status = str(projection.get("status") or "unknown")
    problem_summary = dict(projection.get("problem_summary") or {})
    problems = list(projection.get("problems") or [])

    lines = [
        f"problems generated {generated}",
        f"status {status}",
        _problem_summary_line(problem_summary),
    ]

    if not problems:
        lines.append("no problems detected")
        return "\n".join(lines)

    for problem in problems:
        severity = str(problem.get("severity") or "info")
        source = str(problem.get("source") or "collector")
        code = str(problem.get("code") or "unknown")
        detail = str(problem.get("detail") or "<no detail>")
        lines.append(f"- {severity} {source} {code}: {detail}")
    return "\n".join(lines)


def _line_for_event_group(group: dict[str, object]) -> str:
    bits = [
        str(group.get("env") or "<env>"),
        str(group.get("source") or "<source>"),
        str(group.get("kind") or "<kind>"),
        str(group.get("name") or "<name>"),
    ]
    if group.get("route"):
        bits.append(f"route={group['route']}")
    if group.get("method"):
        bits.append(f"method={group['method']}")
    if group.get("status") is not None:
        bits.append(f"status={group['status']}")
    bits.append(f"count={group.get('count', 0)}")
    bits.append(f"last={group.get('last_seen_utc', '')}")
    return "- " + " ".join(bits)


def _line_for_event(event: dict[str, object]) -> str:
    bits = [
        str(event.get("ts") or event.get("received_at_utc") or ""),
        str(event.get("env") or "<env>"),
        str(event.get("source") or "<source>"),
        str(event.get("kind") or "<kind>"),
        str(event.get("name") or "<name>"),
    ]
    if event.get("route"):
        bits.append(f"route={event['route']}")
    if event.get("method"):
        bits.append(f"method={event['method']}")
    if event.get("status") is not None:
        bits.append(f"status={event['status']}")
    if event.get("duration_ms") is not None:
        bits.append(f"duration_ms={event['duration_ms']}")
    if event.get("detail"):
        bits.append(f"detail={event['detail']}")
    labels = dict(event.get("labels") or {})
    if labels:
        label_text = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
        bits.append(f"labels={label_text}")
    return "- " + " ".join(bits)


def format_events(payload: dict[str, object]) -> str:
    generated = str(payload.get("generated_at_utc") or "")
    summary = list(payload.get("summary") or [])
    recent = list(payload.get("recent") or [])
    lines = [
        f"events generated {generated}",
        f"ingest enabled {'yes' if payload.get('ingest_enabled') else 'no'}",
        (
            f"received total={int(payload.get('received_total') or 0)} "
            f"retained={int(payload.get('retained_total') or 0)} "
            f"retention={format_duration(payload.get('retention_seconds'))}"
        ),
    ]

    if summary:
        lines.append("summary:")
        for group in summary:
            lines.append(_line_for_event_group(group))
    else:
        lines.append("summary: no recent event groups")

    if recent:
        lines.append("recent:")
        for event in recent:
            lines.append(_line_for_event(event))
    else:
        lines.append("recent: no recent events")
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
