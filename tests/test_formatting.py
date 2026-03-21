from __future__ import annotations

import unittest

from tg_box_reporter.formatting import format_problems, format_report, format_summary, split_message


class FormattingTests(unittest.TestCase):
    def test_format_report_includes_host_and_container_summary(self) -> None:
        snapshot = {
            "generated_at_utc": "2026-03-21T00:00:00Z",
            "host": {
                "hostname": "box-1",
                "uptime_seconds": 3661,
                "load_1m": 0.25,
                "load_5m": 0.5,
                "load_15m": 0.75,
                "cpu_count": 4,
                "memory": {"used_bytes": 2 * 1024**3, "total_bytes": 8 * 1024**3, "used_percent": 25.0},
                "swap": {"used_bytes": 0, "total_bytes": 2 * 1024**3, "used_percent": 0.0},
                "disk": {"used_bytes": 10 * 1024**3, "total_bytes": 100 * 1024**3, "used_percent": 10.0, "path": "/"},
            },
            "docker": {
                "available": True,
                "summary": {"total": 2, "running": 1, "restarting": 0, "unhealthy": 0, "exited": 1},
                "containers": [
                    {"name": "web", "cpu_percent": 10.0, "mem_percent": 20.0, "restart_count": 0, "status": "running", "health": "healthy"},
                    {"name": "worker", "cpu_percent": 3.0, "mem_percent": 15.0, "restart_count": 1, "status": "exited", "health": ""},
                ],
            },
            "errors": [],
        }

        rendered = format_report(snapshot, max_containers=5)

        self.assertIn("host box-1 snapshot 2026-03-21T00:00:00Z", rendered)
        self.assertIn("containers total=2 running=1", rendered)
        self.assertIn("- web cpu=10.0% mem=20.0% restarts=0 state=running/healthy", rendered)

    def test_split_message_breaks_large_payload_on_line_boundaries(self) -> None:
        text = "\n".join(f"line-{index}" for index in range(50))
        parts = split_message(text, limit=40)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 40 for part in parts))

    def test_format_summary_includes_problem_counts(self) -> None:
        snapshot = {
            "generated_at_utc": "2026-03-21T00:00:00Z",
            "status": "warning",
            "problem_summary": {"total": 2, "critical": 0, "warning": 2, "info": 0},
            "host": {
                "hostname": "box-1",
                "uptime_seconds": 3661,
                "load_1m": 0.25,
                "load_5m": 0.5,
                "load_15m": 0.75,
                "cpu_count": 4,
                "memory": {"used_bytes": 2 * 1024**3, "total_bytes": 8 * 1024**3, "used_percent": 25.0},
                "swap": {"used_bytes": 0, "total_bytes": 2 * 1024**3, "used_percent": 0.0},
                "disk": {"used_bytes": 10 * 1024**3, "total_bytes": 100 * 1024**3, "used_percent": 10.0, "path": "/"},
            },
            "docker": {
                "available": True,
                "summary": {"total": 2, "running": 1, "restarting": 0, "unhealthy": 0, "exited": 1},
                "containers": [],
            },
        }

        rendered = format_summary(snapshot)

        self.assertIn("summary generated 2026-03-21T00:00:00Z", rendered)
        self.assertIn("status warning", rendered)
        self.assertIn("problems total=2 critical=0 warning=2 info=0", rendered)
        self.assertIn("containers total=2 running=1", rendered)

    def test_format_problems_includes_problem_details(self) -> None:
        snapshot = {
            "generated_at_utc": "2026-03-21T00:00:00Z",
            "status": "critical",
            "problem_summary": {"total": 1, "critical": 1, "warning": 0, "info": 0},
            "problems": [
                {
                    "severity": "critical",
                    "source": "container:web",
                    "code": "container_unhealthy",
                    "detail": "container web is unhealthy",
                }
            ],
        }

        rendered = format_problems(snapshot)

        self.assertIn("problems generated 2026-03-21T00:00:00Z", rendered)
        self.assertIn("status critical", rendered)
        self.assertIn("problems total=1 critical=1 warning=0 info=0", rendered)
        self.assertIn("- critical container:web container_unhealthy: container web is unhealthy", rendered)


if __name__ == "__main__":
    unittest.main()
