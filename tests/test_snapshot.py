from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tg_box_reporter.config import CollectorConfig
from tg_box_reporter.snapshot import SnapshotCollector


class SnapshotCollectorTests(unittest.TestCase):
    def test_collect_merges_host_and_docker_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            proc_root = Path(tmp_dir) / "proc"
            (proc_root / "sys/kernel").mkdir(parents=True)
            (proc_root / "sys/kernel/hostname").write_text("host-a\n", encoding="utf-8")
            (proc_root / "uptime").write_text("3600.00 0.00\n", encoding="utf-8")
            (proc_root / "loadavg").write_text("0.10 0.20 0.30 1/100 123\n", encoding="utf-8")
            (proc_root / "meminfo").write_text(
                "MemTotal:       8192000 kB\n"
                "MemAvailable:   4096000 kB\n"
                "SwapTotal:      2048000 kB\n"
                "SwapFree:       2048000 kB\n",
                encoding="utf-8",
            )

            outputs = {
                ("docker", "ps", "-aq"): "abc1234567890\n",
                ("docker", "inspect", "abc1234567890"): json.dumps(
                    [
                        {
                            "Id": "abc1234567890",
                            "Name": "/vote-mcp-web",
                            "Config": {
                                "Image": "vote-mcp/test:latest",
                                "Labels": {
                                    "com.docker.compose.project": "vote-mcp",
                                    "com.docker.compose.service": "web",
                                },
                            },
                            "State": {
                                "Status": "running",
                                "StartedAt": "2026-03-21T00:00:00Z",
                                "Health": {"Status": "healthy"},
                            },
                            "RestartCount": 2,
                        }
                    ]
                ),
                ("docker", "stats", "--no-stream", "--format", "{{json .}}"): json.dumps(
                    {
                        "Container": "abc123456789",
                        "Name": "vote-mcp-web",
                        "CPUPerc": "12.5%",
                        "MemPerc": "42.0%",
                    }
                )
                + "\n",
            }

            def fake_run(args: list[str]) -> str:
                key = tuple(args)
                if key not in outputs:
                    raise AssertionError(f"unexpected command: {args}")
                return outputs[key]

            config = CollectorConfig(
                bind_host="127.0.0.1",
                port=9707,
                cache_seconds=0,
                host_proc=str(proc_root),
                host_root=tmp_dir,
                disk_path=tmp_dir,
                docker_bin="docker",
                include_stopped=True,
                name_include_regex="",
                name_exclude_regex="",
                require_docker=False,
            )

            snapshot = SnapshotCollector(config, run_command=fake_run).collect()

        self.assertEqual(snapshot["host"]["cpu_count"] > 0, True)
        self.assertEqual(snapshot["host"]["hostname"], "host-a")
        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(snapshot["problem_summary"]["total"], 0)
        self.assertEqual(snapshot["docker"]["summary"]["running"], 1)
        self.assertEqual(snapshot["docker"]["containers"][0]["name"], "vote-mcp-web")
        self.assertEqual(snapshot["docker"]["containers"][0]["cpu_percent"], 12.5)
        self.assertEqual(snapshot["docker"]["containers"][0]["restart_count"], 2)

    def test_collect_flags_host_and_container_problems(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            proc_root = Path(tmp_dir) / "proc"
            proc_root.mkdir()
            (proc_root / "uptime").write_text("120.00 0.00\n", encoding="utf-8")
            (proc_root / "loadavg").write_text("8.00 4.00 2.00 1/100 123\n", encoding="utf-8")
            (proc_root / "meminfo").write_text(
                "MemTotal:       8192000 kB\n"
                "MemAvailable:    512000 kB\n"
                "SwapTotal:      2048000 kB\n"
                "SwapFree:        128000 kB\n",
                encoding="utf-8",
            )

            outputs = {
                ("docker", "ps", "-aq"): "abc1234567890\n",
                ("docker", "inspect", "abc1234567890"): json.dumps(
                    [
                        {
                            "Id": "abc1234567890",
                            "Name": "/busy-worker",
                            "Config": {"Image": "busy:latest", "Labels": {}},
                            "State": {
                                "Status": "restarting",
                                "StartedAt": "2026-03-21T00:00:00Z",
                                "Health": {"Status": "unhealthy"},
                            },
                            "RestartCount": 9,
                        }
                    ]
                ),
                ("docker", "stats", "--no-stream", "--format", "{{json .}}"): json.dumps(
                    {
                        "Container": "abc123456789",
                        "Name": "busy-worker",
                        "CPUPerc": "95.0%",
                        "MemPerc": "92.0%",
                    }
                )
                + "\n",
            }

            def fake_run(args: list[str]) -> str:
                key = tuple(args)
                if key not in outputs:
                    raise AssertionError(f"unexpected command: {args}")
                return outputs[key]

            config = CollectorConfig(
                bind_host="127.0.0.1",
                port=9707,
                cache_seconds=0,
                host_proc=str(proc_root),
                host_root=tmp_dir,
                disk_path=tmp_dir,
                docker_bin="docker",
                include_stopped=True,
                name_include_regex="",
                name_exclude_regex="",
                require_docker=False,
                alert_load_per_cpu_gt=0.1,
                alert_mem_percent_gt=10.0,
                alert_swap_used_mb_gt=10.0,
                alert_disk_percent_gt=-1.0,
                alert_container_restart_count_gt=1,
                alert_container_cpu_percent_gt=10.0,
                alert_container_mem_percent_gt=10.0,
            )

            snapshot = SnapshotCollector(config, run_command=fake_run).collect()

        self.assertEqual(snapshot["status"], "critical")
        self.assertGreaterEqual(snapshot["problem_summary"]["critical"], 2)
        codes = {problem["code"] for problem in snapshot["problems"]}
        self.assertIn("high_load_per_cpu", codes)
        self.assertIn("high_memory_percent", codes)
        self.assertIn("high_swap_used_mb", codes)
        self.assertIn("container_unhealthy", codes)
        self.assertIn("container_restarting", codes)
        self.assertIn("container_restart_count_high", codes)
        self.assertIn("container_cpu_high", codes)
        self.assertIn("container_memory_high", codes)

    def test_collect_falls_back_to_host_root_hostname_when_proc_hostname_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            proc_root = tmp_path / "proc"
            etc_root = tmp_path / "etc"
            proc_root.mkdir()
            etc_root.mkdir()
            (etc_root / "hostname").write_text("host-b\n", encoding="utf-8")
            (proc_root / "uptime").write_text("120.00 0.00\n", encoding="utf-8")
            (proc_root / "loadavg").write_text("0.10 0.20 0.30 1/100 123\n", encoding="utf-8")
            (proc_root / "meminfo").write_text(
                "MemTotal:       8192000 kB\n"
                "MemAvailable:   4096000 kB\n"
                "SwapTotal:      2048000 kB\n"
                "SwapFree:       2048000 kB\n",
                encoding="utf-8",
            )

            config = CollectorConfig(
                bind_host="127.0.0.1",
                port=9707,
                cache_seconds=0,
                host_proc=str(proc_root),
                host_root=tmp_dir,
                disk_path=tmp_dir,
                docker_bin="docker",
                include_stopped=True,
                name_include_regex="",
                name_exclude_regex="",
                require_docker=False,
            )

            snapshot = SnapshotCollector(config, run_command=lambda _args: "").collect()

        self.assertEqual(snapshot["host"]["hostname"], "host-b")

    def test_collect_degrades_cleanly_when_docker_command_times_out(self) -> None:
        observed: list[tuple[tuple[str, ...], float | None]] = []

        with tempfile.TemporaryDirectory() as tmp_dir:
            proc_root = Path(tmp_dir) / "proc"
            proc_root.mkdir()
            (proc_root / "uptime").write_text("120.00 0.00\n", encoding="utf-8")
            (proc_root / "loadavg").write_text("0.10 0.20 0.30 1/100 123\n", encoding="utf-8")
            (proc_root / "meminfo").write_text(
                "MemTotal:       8192000 kB\n"
                "MemAvailable:   4096000 kB\n"
                "SwapTotal:      2048000 kB\n"
                "SwapFree:       2048000 kB\n",
                encoding="utf-8",
            )

            def fake_run(args: list[str], timeout_seconds: float | None = None) -> str:
                observed.append((tuple(args), timeout_seconds))
                raise RuntimeError(f"command timed out after {timeout_seconds:.1f}s: {' '.join(args)}")

            config = CollectorConfig(
                bind_host="127.0.0.1",
                port=9707,
                cache_seconds=0,
                host_proc=str(proc_root),
                host_root=tmp_dir,
                disk_path=tmp_dir,
                docker_bin="docker",
                include_stopped=True,
                name_include_regex="",
                name_exclude_regex="",
                require_docker=False,
                docker_command_timeout_seconds=7.0,
            )

            snapshot = SnapshotCollector(config, run_command=fake_run).collect()

        self.assertEqual(snapshot["docker"]["available"], False)
        self.assertIn("timed out after 7.0s", snapshot["docker"]["error"])
        self.assertEqual(observed, [(("docker", "ps", "-aq"), 7.0)])
        self.assertEqual(snapshot["errors"][0]["source"], "docker")
        self.assertEqual(snapshot["problems"][0]["code"], "docker_unavailable")

    def test_collect_passes_timeout_to_keyword_only_runner(self) -> None:
        observed: list[tuple[tuple[str, ...], float | None]] = []

        with tempfile.TemporaryDirectory() as tmp_dir:
            proc_root = Path(tmp_dir) / "proc"
            proc_root.mkdir()
            (proc_root / "uptime").write_text("120.00 0.00\n", encoding="utf-8")
            (proc_root / "loadavg").write_text("0.10 0.20 0.30 1/100 123\n", encoding="utf-8")
            (proc_root / "meminfo").write_text(
                "MemTotal:       8192000 kB\n"
                "MemAvailable:   4096000 kB\n"
                "SwapTotal:      2048000 kB\n"
                "SwapFree:       2048000 kB\n",
                encoding="utf-8",
            )

            def fake_run(args: list[str], *, timeout_seconds: float | None = None) -> str:
                observed.append((tuple(args), timeout_seconds))
                return ""

            config = CollectorConfig(
                bind_host="127.0.0.1",
                port=9707,
                cache_seconds=0,
                host_proc=str(proc_root),
                host_root=tmp_dir,
                disk_path=tmp_dir,
                docker_bin="docker",
                include_stopped=True,
                name_include_regex="",
                name_exclude_regex="",
                require_docker=False,
                docker_command_timeout_seconds=7.0,
            )

            snapshot = SnapshotCollector(config, run_command=fake_run).collect()

        self.assertEqual(snapshot["docker"]["available"], True)
        self.assertEqual(observed, [(("docker", "ps", "-aq"), 7.0)])


if __name__ == "__main__":
    unittest.main()
