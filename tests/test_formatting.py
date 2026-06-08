from __future__ import annotations

import unittest

from tg_box_reporter.formatting import (
    format_alert_record,
    format_alerts,
    format_events,
    format_problems,
    format_report,
    format_summary,
    split_message,
)


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

        self.assertIn("System report for box-1", rendered)
        self.assertIn("Generated at: 2026-03-21T00:00:00Z", rendered)
        self.assertIn("Container health:", rendered)
        self.assertIn("- Total containers: 2", rendered)
        self.assertIn("- web is running and healthy. CPU usage is 10.0%, memory usage is 20.0%, and restart count is 0.", rendered)

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

        self.assertIn("System summary for box-1", rendered)
        self.assertIn("Overall status: warning", rendered)
        self.assertIn("Problem summary:", rendered)
        self.assertIn("- Warning: 2", rendered)
        self.assertIn("Container health:", rendered)
        self.assertIn("- Total containers: 2", rendered)

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

        self.assertIn("Problems report", rendered)
        self.assertIn("Overall status: critical", rendered)
        self.assertIn("Problem 1:", rendered)
        self.assertIn("Severity: critical", rendered)
        self.assertIn("Code: container_unhealthy", rendered)
        self.assertIn("Detail: container web is unhealthy", rendered)

    def test_format_events_includes_summary_and_recent_items(self) -> None:
        payload = {
            "generated_at_utc": "2026-03-21T00:00:00Z",
            "ingest_enabled": True,
            "received_total": 3,
            "retained_total": 2,
            "retention_seconds": 3600,
            "summary": [
                {
                    "source": "vote-mcp",
                    "env": "prod",
                    "kind": "http.request",
                    "name": "polls_hit",
                    "route": "/polls",
                    "method": "GET",
                    "count": 2,
                    "last_seen_utc": "2026-03-21T00:00:00Z",
                }
            ],
            "recent": [
                {
                    "source": "vote-mcp",
                    "env": "prod",
                    "kind": "http.request",
                    "name": "polls_hit",
                    "route": "/polls",
                    "method": "GET",
                    "status": 200,
                    "ts": "2026-03-21T00:00:00Z",
                }
            ],
        }

        rendered = format_events(payload)

        self.assertIn("Recent application events", rendered)
        self.assertIn("Event ingestion enabled: yes", rendered)
        self.assertIn("In Prod, vote-mcp handled GET /polls for event polls_hit", rendered)
        self.assertIn("At 2026-03-21T00:00:00Z, Prod vote-mcp handled GET /polls", rendered)

    def test_format_alerts_includes_feed_metadata_and_items(self) -> None:
        payload = {
            "generated_at_utc": "2026-03-21T00:00:00Z",
            "alerts_enabled": True,
            "emitted_total": 2,
            "retained_total": 1,
            "retention_seconds": 86400,
            "oldest_seq": 2,
            "latest_seq": 2,
            "after": 1,
            "truncated": False,
            "alerts": [
                {
                    "seq": 2,
                    "alert_class": "route_error_rate_high",
                    "transition": "opened",
                    "severity": "warning",
                    "env": "prod",
                    "source": "vote-mcp",
                    "method": "GET",
                    "route": "/polls",
                    "name": "polls_hit",
                    "summary": "prod GET /polls error rate 2/3 in 5m0s",
                    "detail": "vote-mcp GET /polls saw 2 matching errors across 3 requests in the last 5m0s",
                    "labels": {"kind": "http.request", "status": "500"},
                }
            ],
        }

        rendered = format_alerts(payload)

        self.assertIn("Alert feed", rendered)
        self.assertIn("Alerts enabled: yes", rendered)
        self.assertIn("Requested after sequence: 1", rendered)
        self.assertIn("Alert: Elevated server error rate on Prod", rendered)
        self.assertIn("prod GET /polls error rate 2/3 in 5m0s", rendered)

    def test_format_alert_record_renders_summary_and_stats(self) -> None:
        rendered = format_alert_record(
            {
                "seq": 3,
                "alert_class": "route_seen_after_quiet_period",
                "transition": "noticed",
                "severity": "info",
                "env": "demo",
                "source": "vote-mcp",
                "method": "GET",
                "route": "/polls",
                "summary": "demo GET /polls seen after 6h0m0s quiet",
                "detail": "vote-mcp GET /polls received traffic after 6h0m0s without a matching event",
                "stats": {"observed_quiet_seconds": 21600},
            }
        )

        self.assertIn("Alert: Demo route seen after quiet period", rendered)
        self.assertIn("Route: GET /polls", rendered)
        self.assertIn("Alert metadata:", rendered)
        self.assertIn("Alert stats:", rendered)
        self.assertIn("- Observed quiet period: 6h0m0s", rendered)

    def test_format_alert_record_renders_synthetic_check_target(self) -> None:
        rendered = format_alert_record(
            {
                "seq": 4,
                "alert_class": "synthetic_check_failed",
                "transition": "opened",
                "severity": "warning",
                "env": "prod",
                "source": "vote-mcp-synthetic",
                "target": "prod",
                "name": "happypath",
                "summary": "prod happypath synthetic check failed for prod",
                "detail": "walkthrough failed",
                "labels": {"kind": "synthetic.check", "status": "500", "result": "failed", "target": "prod"},
                "stats": {"duration_ms": "1200", "result": "failed", "target": "prod"},
            }
        )

        self.assertIn("Alert: Synthetic check failed on Prod", rendered)
        self.assertIn("Synthetic target: prod", rendered)
        self.assertNotIn("Route:", rendered)
        self.assertIn("- Synthetic result: failed", rendered)
        self.assertIn("- Duration: 1200 ms", rendered)


if __name__ == "__main__":
    unittest.main()
