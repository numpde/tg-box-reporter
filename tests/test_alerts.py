from __future__ import annotations

import unittest

from tg_box_reporter.alerts import (
    AlertRuleEngine,
    CollectorAlertsConfig,
    RouteErrorRateHighConfig,
    RouteSeenAfterQuietConfig,
)


class AlertRuleEngineTests(unittest.TestCase):
    def test_route_error_rate_high_opens_and_resolves(self) -> None:
        engine = AlertRuleEngine(
            CollectorAlertsConfig(
                enabled=True,
                route_error_rate_high=RouteErrorRateHighConfig(
                    enabled=True,
                    window_seconds=60,
                    min_requests=3,
                    min_errors=2,
                    error_rate_gt=0.6,
                    clear_rate_lt=0.55,
                    status_classes=("5xx",),
                ),
            ),
            now_utc=lambda: "2026-03-21T00:00:00Z",
        )

        base_event = {
            "source": "vote-mcp",
            "env": "demo",
            "kind": "http.request",
            "name": "poll_update",
            "route": "/api/v1/polls/{poll_id}",
            "method": "GET",
        }

        self.assertEqual(engine.evaluate({**base_event, "status": 503, "ts": "2026-03-21T00:00:01Z"}, now=1.0), [])
        self.assertEqual(engine.evaluate({**base_event, "status": 503, "ts": "2026-03-21T00:00:02Z"}, now=2.0), [])

        opened = engine.evaluate({**base_event, "status": 200, "ts": "2026-03-21T00:00:03Z"}, now=3.0)
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["alert_class"], "route_error_rate_high")
        self.assertEqual(opened[0]["transition"], "opened")
        self.assertEqual(opened[0]["stats"]["error_requests"], 2)
        self.assertEqual(opened[0]["stats"]["total_requests"], 3)

        resolved = engine.evaluate({**base_event, "status": 200, "ts": "2026-03-21T00:00:04Z"}, now=4.0)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["alert_class"], "route_error_rate_high")
        self.assertEqual(resolved[0]["transition"], "resolved")
        self.assertEqual(resolved[0]["stats"]["error_requests"], 2)
        self.assertEqual(resolved[0]["stats"]["total_requests"], 4)

    def test_route_seen_after_quiet_does_not_fire_on_first_seen(self) -> None:
        engine = AlertRuleEngine(
            CollectorAlertsConfig(
                enabled=True,
                route_seen_after_quiet=RouteSeenAfterQuietConfig(
                    enabled=True,
                    quiet_period_seconds=10,
                    emit_on_first_seen=False,
                ),
            ),
            now_utc=lambda: "2026-03-21T00:00:00Z",
        )

        event = {
            "source": "vote-mcp",
            "env": "prod",
            "kind": "http.request",
            "name": "poll_results",
            "route": "/api/v1/polls/{poll_id}/results",
            "method": "GET",
            "ts": "2026-03-21T00:00:00Z",
        }

        self.assertEqual(engine.evaluate(event, now=0.0), [])
        self.assertEqual(engine.evaluate({**event, "ts": "2026-03-21T00:00:05Z"}, now=5.0), [])

        noticed = engine.evaluate({**event, "ts": "2026-03-21T00:00:20Z"}, now=20.0)
        self.assertEqual(len(noticed), 1)
        self.assertEqual(noticed[0]["alert_class"], "route_seen_after_quiet_period")
        self.assertEqual(noticed[0]["transition"], "noticed")
        self.assertEqual(noticed[0]["stats"]["observed_quiet_seconds"], 15)


if __name__ == "__main__":
    unittest.main()
