from __future__ import annotations

from typing import Callable

from .snapshot import sort_containers

QueryParams = dict[str, list[str]]
ProjectionBuilder = Callable[[dict[str, object], QueryParams], dict[str, object]]


def parse_limit(query: QueryParams, *, default: int = 50, maximum: int = 500) -> int:
    raw = query.get("limit", [""])[0].strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, min(value, maximum))


def project_ready(snapshot: dict[str, object], _query: QueryParams | None = None) -> dict[str, object]:
    return {
        "ok": True,
        "status": snapshot.get("status", "unknown"),
        "generated_at_utc": snapshot.get("generated_at_utc"),
    }


def project_summary(snapshot: dict[str, object], _query: QueryParams | None = None) -> dict[str, object]:
    host = dict(snapshot.get("host") or {})
    docker = dict(snapshot.get("docker") or {})
    return {
        "generated_at_utc": snapshot.get("generated_at_utc"),
        "status": snapshot.get("status"),
        "problem_summary": snapshot.get("problem_summary"),
        "host": {
            "hostname": host.get("hostname"),
            "cpu_count": host.get("cpu_count"),
            "uptime_seconds": host.get("uptime_seconds"),
            "load_1m": host.get("load_1m"),
            "load_5m": host.get("load_5m"),
            "load_15m": host.get("load_15m"),
            "memory": host.get("memory"),
            "swap": host.get("swap"),
            "disk": host.get("disk"),
        },
        "docker": {
            "available": docker.get("available"),
            "source": docker.get("source"),
            "summary": docker.get("summary"),
        },
    }


def project_containers(snapshot: dict[str, object], query: QueryParams | None = None) -> dict[str, object]:
    resolved_query = query or {}
    docker = dict(snapshot.get("docker") or {})
    containers = sort_containers(list(docker.get("containers") or []))
    limit = parse_limit(resolved_query)
    return {
        "generated_at_utc": snapshot.get("generated_at_utc"),
        "status": snapshot.get("status"),
        "problem_summary": snapshot.get("problem_summary"),
        "total": len(containers),
        "containers": containers[:limit],
    }


def project_problems(snapshot: dict[str, object], _query: QueryParams | None = None) -> dict[str, object]:
    return {
        "generated_at_utc": snapshot.get("generated_at_utc"),
        "status": snapshot.get("status"),
        "problem_summary": snapshot.get("problem_summary"),
        "problems": snapshot.get("problems", []),
    }


def project_snapshot(snapshot: dict[str, object], _query: QueryParams | None = None) -> dict[str, object]:
    return snapshot


SNAPSHOT_PROJECTIONS: dict[str, ProjectionBuilder] = {
    "/readyz": project_ready,
    "/snapshot": project_snapshot,
    "/summary": project_summary,
    "/containers": project_containers,
    "/problems": project_problems,
}
