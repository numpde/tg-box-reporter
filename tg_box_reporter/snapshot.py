from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from datetime import datetime, timezone
from inspect import Parameter, signature
from pathlib import Path
from typing import Callable

from .config import CollectorConfig


class CommandExecutionError(RuntimeError):
    pass


CommandRunner = Callable[..., str]


def _default_run_command(args: list[str], timeout_seconds: float | None = None) -> str:
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandExecutionError(
            f"command timed out after {timeout_seconds:.1f}s: {' '.join(args)}"
        ) from exc
    if completed.returncode != 0:
        detail = " ".join(part for part in [completed.stdout.strip(), completed.stderr.strip()] if part)
        if not detail:
            detail = "<no output>"
        raise CommandExecutionError(f"command failed: {' '.join(args)} (exit={completed.returncode}) {detail}")
    return completed.stdout


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_first_existing(paths: list[Path]) -> str:
    for path in paths:
        try:
            value = _read_text(path).strip()
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
            continue
        if value:
            return value
    return ""


def _percent(value: str) -> float | None:
    trimmed = value.strip().rstrip("%")
    if not trimmed:
        return None
    try:
        return float(trimmed)
    except ValueError:
        return None


def _parse_meminfo(raw: str) -> dict[str, int]:
    parsed: dict[str, int] = {}
    for line in raw.splitlines():
        key, _, remainder = line.partition(":")
        parts = remainder.strip().split()
        if not key or not parts:
            continue
        try:
            value = int(parts[0])
        except ValueError:
            continue
        parsed[key] = value * 1024 if len(parts) > 1 and parts[1].lower() == "kb" else value
    return parsed


def sort_containers(containers: list[dict[str, object]]) -> list[dict[str, object]]:
    return sorted(
        containers,
        key=lambda item: (
            float(item.get("cpu_percent") or 0.0),
            float(item.get("mem_percent") or 0.0),
            float(item.get("restart_count") or 0.0),
            str(item.get("name") or ""),
        ),
        reverse=True,
    )


def _problem_summary(problems: list[dict[str, object]]) -> dict[str, int]:
    critical = sum(1 for item in problems if item.get("severity") == "critical")
    warning = sum(1 for item in problems if item.get("severity") == "warning")
    info = sum(1 for item in problems if item.get("severity") == "info")
    return {
        "total": len(problems),
        "critical": critical,
        "warning": warning,
        "info": info,
    }


def _snapshot_status(problems: list[dict[str, object]]) -> str:
    if any(item.get("severity") == "critical" for item in problems):
        return "critical"
    if any(item.get("severity") == "warning" for item in problems):
        return "warning"
    return "ok"


def _docker_payload(
    *,
    available: bool,
    containers: list[dict[str, object]] | None = None,
    error: str = "",
) -> dict[str, object]:
    return {
        "available": available,
        "source": "docker-cli",
        "error": error,
        "containers": containers or [],
        "summary": {
            "total": len(containers or []),
            "running": sum(1 for item in (containers or []) if item.get("status") == "running"),
            "restarting": sum(1 for item in (containers or []) if item.get("status") == "restarting"),
            "unhealthy": sum(1 for item in (containers or []) if item.get("health") == "unhealthy"),
            "exited": sum(1 for item in (containers or []) if item.get("status") == "exited"),
        },
    }


def _make_problem(
    *,
    severity: str,
    source: str,
    code: str,
    detail: str,
    value: object | None = None,
    threshold: object | None = None,
) -> dict[str, object]:
    problem = {
        "severity": severity,
        "source": source,
        "code": code,
        "detail": detail,
    }
    if value is not None:
        problem["value"] = value
    if threshold is not None:
        problem["threshold"] = threshold
    return problem


class SnapshotCollector:
    def __init__(self, config: CollectorConfig, *, run_command: CommandRunner | None = None):
        self.config = config
        self.run_command = run_command or _default_run_command
        self._run_command_signature = self._resolve_signature(self.run_command)
        self._include_pattern = re.compile(config.name_include_regex) if config.name_include_regex else None
        self._exclude_pattern = re.compile(config.name_exclude_regex) if config.name_exclude_regex else None

    @staticmethod
    def _resolve_signature(run_command: CommandRunner):
        try:
            return signature(run_command)
        except (TypeError, ValueError):
            return None

    def _run_command(self, args: list[str]) -> str:
        if self._run_command_signature is None:
            return self.run_command(args, self.config.docker_command_timeout_seconds)

        parameters = list(self._run_command_signature.parameters.values())
        if any(parameter.kind == Parameter.VAR_KEYWORD for parameter in parameters):
            return self.run_command(args, timeout_seconds=self.config.docker_command_timeout_seconds)
        if any(
            parameter.name == "timeout_seconds"
            for parameter in parameters[1:]
        ):
            return self.run_command(args, timeout_seconds=self.config.docker_command_timeout_seconds)
        if any(parameter.kind == Parameter.VAR_POSITIONAL for parameter in parameters):
            return self.run_command(args, self.config.docker_command_timeout_seconds)
        if len(parameters) >= 2 and parameters[1].kind in {
            Parameter.POSITIONAL_ONLY,
            Parameter.POSITIONAL_OR_KEYWORD,
        }:
            return self.run_command(args, self.config.docker_command_timeout_seconds)
        return self.run_command(args)

    def collect(self) -> dict[str, object]:
        errors: list[dict[str, str]] = []
        host = self._collect_host()
        docker = self._collect_docker(errors)
        problems = self._collect_problems(host, docker, errors)
        return {
            "generated_at_utc": datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "status": _snapshot_status(problems),
            "host": host,
            "docker": docker,
            "errors": errors,
            "problems": problems,
            "problem_summary": _problem_summary(problems),
        }

    def _collect_host(self) -> dict[str, object]:
        proc_root = Path(self.config.host_proc)
        host_root = Path(self.config.host_root)
        hostname = _read_first_existing(
            [
                host_root / "etc/hostname",
                proc_root / "sys/kernel/hostname",
            ]
        ) or socket.gethostname()
        meminfo = _parse_meminfo(_read_text(proc_root / "meminfo"))
        uptime_seconds = float(_read_text(proc_root / "uptime").split()[0])
        load_parts = _read_text(proc_root / "loadavg").split()
        load_1m = float(load_parts[0])
        load_5m = float(load_parts[1])
        load_15m = float(load_parts[2])

        total_mem = int(meminfo.get("MemTotal", 0))
        available_mem = int(meminfo.get("MemAvailable", 0))
        used_mem = max(0, total_mem - available_mem)
        total_swap = int(meminfo.get("SwapTotal", 0))
        free_swap = int(meminfo.get("SwapFree", 0))
        used_swap = max(0, total_swap - free_swap)

        stat = os.statvfs(self.config.disk_path)
        total_disk = stat.f_blocks * stat.f_frsize
        free_disk = stat.f_bavail * stat.f_frsize
        used_disk = max(0, total_disk - free_disk)

        return {
            "hostname": hostname,
            "host_root": str(host_root),
            "proc_root": str(proc_root),
            "cpu_count": os.cpu_count() or 0,
            "uptime_seconds": uptime_seconds,
            "load_1m": load_1m,
            "load_5m": load_5m,
            "load_15m": load_15m,
            "memory": {
                "total_bytes": total_mem,
                "available_bytes": available_mem,
                "used_bytes": used_mem,
                "used_percent": (used_mem / total_mem * 100.0) if total_mem else 0.0,
            },
            "swap": {
                "total_bytes": total_swap,
                "free_bytes": free_swap,
                "used_bytes": used_swap,
                "used_percent": (used_swap / total_swap * 100.0) if total_swap else 0.0,
            },
            "disk": {
                "path": self.config.disk_path,
                "total_bytes": total_disk,
                "free_bytes": free_disk,
                "used_bytes": used_disk,
                "used_percent": (used_disk / total_disk * 100.0) if total_disk else 0.0,
            },
        }

    def _collect_docker(self, errors: list[dict[str, str]]) -> dict[str, object]:
        ps_args = [self.config.docker_bin, "ps", "-aq" if self.config.include_stopped else "-q"]
        try:
            container_ids = [line.strip() for line in self._run_command(ps_args).splitlines() if line.strip()]
        except Exception as exc:
            detail = str(exc)
            if self.config.require_docker:
                raise RuntimeError(detail) from exc
            errors.append({"source": "docker", "detail": detail})
            return _docker_payload(available=False, error=detail)

        if not container_ids:
            return _docker_payload(available=True)

        try:
            inspect_output = self._run_command([self.config.docker_bin, "inspect", *container_ids])
            inspect_payload = json.loads(inspect_output)
        except Exception as exc:
            detail = str(exc)
            if self.config.require_docker:
                raise RuntimeError(detail) from exc
            errors.append({"source": "docker-inspect", "detail": detail})
            return _docker_payload(available=False, error=detail)

        stats_by_name: dict[str, dict[str, object]] = {}
        stats_by_id: dict[str, dict[str, object]] = {}
        try:
            stats_output = self._run_command(
                [
                    self.config.docker_bin,
                    "stats",
                    "--no-stream",
                    "--format",
                    "{{json .}}",
                ]
            )
        except Exception as exc:
            stats_output = ""
            errors.append({"source": "docker-stats", "detail": str(exc)})

        for line in stats_output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                errors.append({"source": "docker-stats", "detail": f"invalid JSON from docker stats: {line}"})
                continue
            name = str(parsed.get("Name") or "").lstrip("/")
            container_id = str(parsed.get("Container") or "")
            stats = {
                "cpu_percent": _percent(str(parsed.get("CPUPerc") or "")),
                "mem_percent": _percent(str(parsed.get("MemPerc") or "")),
                "mem_usage": str(parsed.get("MemUsage") or ""),
                "net_io": str(parsed.get("NetIO") or ""),
                "block_io": str(parsed.get("BlockIO") or ""),
                "pids": str(parsed.get("PIDs") or ""),
            }
            if name:
                stats_by_name[name] = stats
            if container_id:
                stats_by_id[container_id] = stats

        containers: list[dict[str, object]] = []
        for item in inspect_payload:
            container_id = str(item.get("Id") or "")
            short_id = container_id[:12]
            name = str(item.get("Name") or short_id).lstrip("/")
            if not self._container_allowed(name):
                continue
            state = dict(item.get("State") or {})
            health = dict(state.get("Health") or {})
            labels = dict((item.get("Config") or {}).get("Labels") or {})
            stats = stats_by_name.get(name) or stats_by_id.get(short_id) or {}
            containers.append(
                {
                    "id": short_id,
                    "name": name,
                    "image": str((item.get("Config") or {}).get("Image") or ""),
                    "status": str(state.get("Status") or "unknown"),
                    "health": str(health.get("Status") or ""),
                    "restart_count": int(item.get("RestartCount") or 0),
                    "started_at": str(state.get("StartedAt") or ""),
                    "cpu_percent": stats.get("cpu_percent"),
                    "mem_percent": stats.get("mem_percent"),
                    "compose_project": str(labels.get("com.docker.compose.project") or ""),
                    "compose_service": str(labels.get("com.docker.compose.service") or ""),
                }
            )

        return _docker_payload(available=True, containers=sort_containers(containers))

    def _container_allowed(self, name: str) -> bool:
        if self._include_pattern and not self._include_pattern.search(name):
            return False
        if self._exclude_pattern and self._exclude_pattern.search(name):
            return False
        return True

    def _collect_problems(
        self,
        host: dict[str, object],
        docker: dict[str, object],
        errors: list[dict[str, str]],
    ) -> list[dict[str, object]]:
        problems: list[dict[str, object]] = []

        cpu_count = int(host.get("cpu_count") or 0)
        load_1m = float(host.get("load_1m") or 0.0)
        load_per_cpu = load_1m / max(cpu_count, 1)
        if self.config.alert_load_per_cpu_gt >= 0 and load_per_cpu > self.config.alert_load_per_cpu_gt:
            problems.append(
                _make_problem(
                    severity="warning",
                    source="host",
                    code="high_load_per_cpu",
                    detail=f"1m load per CPU is high ({load_per_cpu:.2f} > {self.config.alert_load_per_cpu_gt:.2f})",
                    value=round(load_per_cpu, 2),
                    threshold=self.config.alert_load_per_cpu_gt,
                )
            )

        memory = dict(host.get("memory") or {})
        memory_used_percent = float(memory.get("used_percent") or 0.0)
        if self.config.alert_mem_percent_gt >= 0 and memory_used_percent > self.config.alert_mem_percent_gt:
            problems.append(
                _make_problem(
                    severity="warning",
                    source="host",
                    code="high_memory_percent",
                    detail=(
                        f"memory usage is high ({memory_used_percent:.1f}% > "
                        f"{self.config.alert_mem_percent_gt:.1f}%)"
                    ),
                    value=round(memory_used_percent, 1),
                    threshold=self.config.alert_mem_percent_gt,
                )
            )

        swap = dict(host.get("swap") or {})
        swap_used_mb = float(swap.get("used_bytes") or 0.0) / (1024.0 * 1024.0)
        if self.config.alert_swap_used_mb_gt >= 0 and swap_used_mb > self.config.alert_swap_used_mb_gt:
            problems.append(
                _make_problem(
                    severity="warning",
                    source="host",
                    code="high_swap_used_mb",
                    detail=(
                        f"swap usage is high ({swap_used_mb:.1f} MiB > "
                        f"{self.config.alert_swap_used_mb_gt:.1f} MiB)"
                    ),
                    value=round(swap_used_mb, 1),
                    threshold=self.config.alert_swap_used_mb_gt,
                )
            )

        disk = dict(host.get("disk") or {})
        disk_used_percent = float(disk.get("used_percent") or 0.0)
        if self.config.alert_disk_percent_gt >= 0 and disk_used_percent > self.config.alert_disk_percent_gt:
            problems.append(
                _make_problem(
                    severity="warning",
                    source="host",
                    code="high_disk_percent",
                    detail=(
                        f"disk usage is high at {disk.get('path', '<unknown>')} "
                        f"({disk_used_percent:.1f}% > {self.config.alert_disk_percent_gt:.1f}%)"
                    ),
                    value=round(disk_used_percent, 1),
                    threshold=self.config.alert_disk_percent_gt,
                )
            )

        if not docker.get("available", False):
            problems.append(
                _make_problem(
                    severity="warning",
                    source="docker",
                    code="docker_unavailable",
                    detail=str(docker.get("error") or "docker data unavailable"),
                )
            )

        for error in errors:
            source = str(error.get("source") or "collector")
            detail = str(error.get("detail") or "<no detail>")
            if source == "docker":
                continue
            problems.append(_make_problem(severity="warning", source=source, code="collector_error", detail=detail))

        for container in list(docker.get("containers") or []):
            name = str(container.get("name") or "<unknown>")
            status = str(container.get("status") or "")
            health = str(container.get("health") or "")
            restart_count = int(container.get("restart_count") or 0)
            cpu_percent = float(container.get("cpu_percent") or 0.0)
            mem_percent = float(container.get("mem_percent") or 0.0)

            if health == "unhealthy":
                problems.append(
                    _make_problem(
                        severity="critical",
                        source=f"container:{name}",
                        code="container_unhealthy",
                        detail=f"container {name} is unhealthy",
                    )
                )
            if status == "restarting":
                problems.append(
                    _make_problem(
                        severity="critical",
                        source=f"container:{name}",
                        code="container_restarting",
                        detail=f"container {name} is restarting",
                    )
                )
            if status == "exited":
                problems.append(
                    _make_problem(
                        severity="warning",
                        source=f"container:{name}",
                        code="container_exited",
                        detail=f"container {name} is exited",
                    )
                )
            if (
                self.config.alert_container_restart_count_gt >= 0
                and restart_count > self.config.alert_container_restart_count_gt
            ):
                problems.append(
                    _make_problem(
                        severity="warning",
                        source=f"container:{name}",
                        code="container_restart_count_high",
                        detail=(
                            f"container {name} restart count is high "
                            f"({restart_count} > {self.config.alert_container_restart_count_gt})"
                        ),
                        value=restart_count,
                        threshold=self.config.alert_container_restart_count_gt,
                    )
                )
            if self.config.alert_container_cpu_percent_gt >= 0 and cpu_percent > self.config.alert_container_cpu_percent_gt:
                problems.append(
                    _make_problem(
                        severity="warning",
                        source=f"container:{name}",
                        code="container_cpu_high",
                        detail=(
                            f"container {name} CPU is high "
                            f"({cpu_percent:.1f}% > {self.config.alert_container_cpu_percent_gt:.1f}%)"
                        ),
                        value=round(cpu_percent, 1),
                        threshold=self.config.alert_container_cpu_percent_gt,
                    )
                )
            if self.config.alert_container_mem_percent_gt >= 0 and mem_percent > self.config.alert_container_mem_percent_gt:
                problems.append(
                    _make_problem(
                        severity="warning",
                        source=f"container:{name}",
                        code="container_memory_high",
                        detail=(
                            f"container {name} memory is high "
                            f"({mem_percent:.1f}% > {self.config.alert_container_mem_percent_gt:.1f}%)"
                        ),
                        value=round(mem_percent, 1),
                        threshold=self.config.alert_container_mem_percent_gt,
                    )
                )

        severity_order = {"critical": 0, "warning": 1, "info": 2}
        return sorted(
            problems,
            key=lambda item: (
                severity_order.get(str(item.get("severity") or "info"), 3),
                str(item.get("source") or ""),
                str(item.get("code") or ""),
            ),
        )
