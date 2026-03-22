from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tg_box_reporter.config import ConfigError, RelayConfig
from tg_box_reporter.relay import CollectorClient, RelayService
from tg_box_reporter.relay_health import RelayHeartbeat, healthcheck_main
from tg_box_reporter.telegram_api import TelegramClient


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def send_message(self, *, chat_id: str, text: str) -> dict[str, object]:
        self.messages.append((chat_id, text))
        return {"ok": True}

    def get_updates(self, *, offset: int | None = None, timeout: int = 30) -> list[dict[str, object]]:
        _ = (offset, timeout)
        return []


class FakeCollectorClient:
    def __init__(
        self,
        snapshot_payload: dict[str, object] | None = None,
        *,
        events_payload: dict[str, object] | None = None,
        alerts_payload: dict[str, object] | None = None,
        snapshot_error: Exception | None = None,
        events_error: Exception | None = None,
        alerts_error: Exception | None = None,
    ):
        self.snapshot_payload = snapshot_payload or {}
        self.events_payload = events_payload or {}
        self.alerts_payload = alerts_payload or {}
        self.snapshot_error = snapshot_error
        self.events_error = events_error
        self.alerts_error = alerts_error
        self.alert_requests: list[tuple[int | None, int | None]] = []

    def fetch_snapshot(self) -> dict[str, object]:
        if self.snapshot_error is not None:
            raise self.snapshot_error
        return self.snapshot_payload

    def fetch_events(self) -> dict[str, object]:
        if self.events_error is not None:
            raise self.events_error
        return self.events_payload

    def fetch_alerts(self, *, after_seq: int | None = None, limit: int | None = None) -> dict[str, object]:
        self.alert_requests.append((after_seq, limit))
        if self.alerts_error is not None:
            raise self.alerts_error
        if callable(self.alerts_payload):
            return self.alerts_payload(after_seq, limit)
        return self.alerts_payload


class FakeHeartbeat:
    def __init__(self) -> None:
        self.calls = 0

    def mark_alive(self) -> None:
        self.calls += 1


class InterruptingTelegram(FakeTelegram):
    def get_updates(self, *, offset: int | None = None, timeout: int = 30) -> list[dict[str, object]]:
        _ = (offset, timeout)
        raise KeyboardInterrupt


class RelayServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = RelayConfig(
            bot_token="token",
            collector_url="http://collector/snapshot",
            mode="polling",
            chat_id="123",
            allowed_chat_ids=("123",),
            interval_seconds=900,
            startup_report=False,
            request_timeout_seconds=20,
            get_updates_timeout_seconds=30,
            max_containers=5,
            telegram_api_base="https://api.telegram.org",
        )
        self.snapshot = {
            "generated_at_utc": "2026-03-21T00:00:00Z",
            "host": {
                "hostname": "box-1",
                "uptime_seconds": 10,
                "load_1m": 0.1,
                "load_5m": 0.2,
                "load_15m": 0.3,
                "cpu_count": 2,
                "memory": {"used_bytes": 1, "total_bytes": 2, "used_percent": 50.0},
                "swap": {"used_bytes": 0, "total_bytes": 0, "used_percent": 0.0},
                "disk": {"used_bytes": 1, "total_bytes": 10, "used_percent": 10.0, "path": "/"},
            },
            "docker": {"available": True, "summary": {"total": 0, "running": 0, "restarting": 0, "unhealthy": 0, "exited": 0}, "containers": []},
            "errors": [],
        }
        self.events = {
            "generated_at_utc": "2026-03-21T00:00:00Z",
            "ingest_enabled": True,
            "received_total": 1,
            "retained_total": 1,
            "retention_seconds": 3600,
            "summary": [
                {
                    "source": "vote-mcp",
                    "env": "prod",
                    "kind": "http.request",
                    "name": "polls_hit",
                    "route": "/polls",
                    "method": "GET",
                    "count": 1,
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
        self.alerts = {
            "generated_at_utc": "2026-03-21T00:00:00Z",
            "alerts_enabled": True,
            "emitted_total": 1,
            "retained_total": 1,
            "retention_seconds": 86400,
            "oldest_seq": 1,
            "latest_seq": 1,
            "after": None,
            "truncated": False,
            "alerts": [
                {
                    "seq": 1,
                    "alert_class": "route_error_rate_high",
                    "transition": "opened",
                    "severity": "warning",
                    "source": "vote-mcp",
                    "env": "prod",
                    "method": "GET",
                    "route": "/polls",
                    "name": "polls_hit",
                    "summary": "prod GET /polls error rate 2/3 in 5m0s",
                    "detail": "vote-mcp GET /polls saw 2 matching errors across 3 requests in the last 5m0s",
                    "stats": {"total_requests": 3, "error_requests": 2},
                }
            ],
        }

    def test_report_command_sends_formatted_report(self) -> None:
        telegram = FakeTelegram()
        service = RelayService(
            self.config,
            telegram=telegram,
            collector_client=FakeCollectorClient(self.snapshot, events_payload=self.events),
            stderr=io.StringIO(),
        )

        service.handle_command("123", "/report")

        self.assertEqual(len(telegram.messages), 1)
        self.assertEqual(telegram.messages[0][0], "123")
        self.assertIn("host box-1", telegram.messages[0][1])

    def test_unauthorized_chat_gets_rejected(self) -> None:
        telegram = FakeTelegram()
        service = RelayService(
            self.config,
            telegram=telegram,
            collector_client=FakeCollectorClient(self.snapshot, events_payload=self.events),
            stderr=io.StringIO(),
        )

        service.handle_command("999", "/report")

        self.assertEqual(telegram.messages, [("999", "unauthorized chat")])

    def test_report_failure_returns_explicit_error_message(self) -> None:
        telegram = FakeTelegram()
        service = RelayService(
            self.config,
            telegram=telegram,
            collector_client=FakeCollectorClient(snapshot_error=RuntimeError("collector down")),
            stderr=io.StringIO(),
        )

        service.handle_command("123", "/report")

        self.assertEqual(telegram.messages, [("123", "report failed: collector down")])

    def test_containers_command_ignores_report_max_container_cap(self) -> None:
        telegram = FakeTelegram()
        snapshot = {
            **self.snapshot,
            "docker": {
                "available": True,
                "summary": {"total": 2, "running": 2, "restarting": 0, "unhealthy": 0, "exited": 0},
                "containers": [
                    {"name": "alpha", "cpu_percent": 2.0, "mem_percent": 1.0, "restart_count": 0, "status": "running", "health": "healthy"},
                    {"name": "beta", "cpu_percent": 1.0, "mem_percent": 1.0, "restart_count": 0, "status": "running", "health": "healthy"},
                ],
            },
        }
        config = RelayConfig(
            bot_token="token",
            collector_url="http://collector/snapshot",
            mode="polling",
            chat_id="123",
            allowed_chat_ids=("123",),
            interval_seconds=900,
            startup_report=False,
            request_timeout_seconds=20,
            get_updates_timeout_seconds=30,
            max_containers=1,
            telegram_api_base="https://api.telegram.org",
        )
        service = RelayService(
            config,
            telegram=telegram,
            collector_client=FakeCollectorClient(snapshot, events_payload=self.events),
            stderr=io.StringIO(),
        )

        service.handle_command("123", "/containers")

        self.assertEqual(len(telegram.messages), 1)
        self.assertIn("alpha", telegram.messages[0][1])
        self.assertIn("beta", telegram.messages[0][1])

    def test_summary_command_sends_compact_summary(self) -> None:
        telegram = FakeTelegram()
        snapshot = {
            **self.snapshot,
            "status": "warning",
            "problem_summary": {"total": 1, "critical": 0, "warning": 1, "info": 0},
            "docker": {
                "available": True,
                "summary": {"total": 2, "running": 2, "restarting": 0, "unhealthy": 0, "exited": 0},
                "containers": [
                    {"name": "alpha", "cpu_percent": 2.0, "mem_percent": 1.0, "restart_count": 0, "status": "running", "health": "healthy"},
                    {"name": "beta", "cpu_percent": 1.0, "mem_percent": 1.0, "restart_count": 0, "status": "running", "health": "healthy"},
                ],
            },
        }
        service = RelayService(
            self.config,
            telegram=telegram,
            collector_client=FakeCollectorClient(snapshot, events_payload=self.events),
            stderr=io.StringIO(),
        )

        service.handle_command("123", "/summary")

        self.assertEqual(len(telegram.messages), 1)
        self.assertIn("status warning", telegram.messages[0][1])
        self.assertIn("problems total=1 critical=0 warning=1 info=0", telegram.messages[0][1])
        self.assertNotIn("top containers:", telegram.messages[0][1])

    def test_problems_command_sends_problem_list(self) -> None:
        telegram = FakeTelegram()
        snapshot = {
            **self.snapshot,
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
        service = RelayService(
            self.config,
            telegram=telegram,
            collector_client=FakeCollectorClient(snapshot, events_payload=self.events),
            stderr=io.StringIO(),
        )

        service.handle_command("123", "/problems")

        self.assertEqual(len(telegram.messages), 1)
        self.assertIn("status critical", telegram.messages[0][1])
        self.assertIn("container_unhealthy", telegram.messages[0][1])

    def test_help_lists_all_commands(self) -> None:
        telegram = FakeTelegram()
        service = RelayService(
            self.config,
            telegram=telegram,
            collector_client=FakeCollectorClient(self.snapshot, events_payload=self.events),
            stderr=io.StringIO(),
        )

        service.handle_command("123", "/help")

        self.assertEqual(
            telegram.messages,
            [("123", "/report\n/summary\n/containers\n/problems\n/events\n/alerts\n/help")],
        )

    def test_events_command_sends_event_summary(self) -> None:
        telegram = FakeTelegram()
        service = RelayService(
            self.config,
            telegram=telegram,
            collector_client=FakeCollectorClient(self.snapshot, events_payload=self.events),
            stderr=io.StringIO(),
        )

        service.handle_command("123", "/events")

        self.assertEqual(len(telegram.messages), 1)
        self.assertIn("events generated", telegram.messages[0][1])
        self.assertIn("polls_hit", telegram.messages[0][1])

    def test_alerts_command_sends_alert_feed(self) -> None:
        telegram = FakeTelegram()
        service = RelayService(
            self.config,
            telegram=telegram,
            collector_client=FakeCollectorClient(
                self.snapshot,
                events_payload=self.events,
                alerts_payload=self.alerts,
            ),
            stderr=io.StringIO(),
        )

        service.handle_command("123", "/alerts")

        self.assertEqual(len(telegram.messages), 1)
        self.assertIn("alerts generated", telegram.messages[0][1])
        self.assertIn("route_error_rate_high", telegram.messages[0][1])

    def test_send_pending_alerts_advances_cursor_without_duplicates(self) -> None:
        telegram = FakeTelegram()

        def alerts_payload(after_seq: int | None, limit: int | None) -> dict[str, object]:
            _ = limit
            if after_seq in (None, 0):
                return {**self.alerts, "after": after_seq, "alerts": list(self.alerts["alerts"])}
            return {**self.alerts, "after": after_seq, "alerts": []}

        service = RelayService(
            RelayConfig(
                bot_token="token",
                collector_url="http://collector/snapshot",
                mode="polling",
                chat_id="123",
                allowed_chat_ids=("123",),
                interval_seconds=900,
                startup_report=False,
                request_timeout_seconds=20,
                get_updates_timeout_seconds=30,
                max_containers=5,
                telegram_api_base="https://api.telegram.org",
                alerts_enabled=True,
                alert_batch_size=10,
            ),
            telegram=telegram,
            collector_client=FakeCollectorClient(
                self.snapshot,
                events_payload=self.events,
                alerts_payload=alerts_payload,
            ),
            stderr=io.StringIO(),
        )

        service.send_pending_alerts()
        service.send_pending_alerts()

        self.assertEqual(len(telegram.messages), 1)
        self.assertEqual(service._last_alert_seq, 1)

    def test_initialize_alert_cursor_baselines_to_latest_seq(self) -> None:
        collector = FakeCollectorClient(
            self.snapshot,
            events_payload=self.events,
            alerts_payload=self.alerts,
        )
        service = RelayService(
            RelayConfig(
                bot_token="token",
                collector_url="http://collector/snapshot",
                mode="polling",
                chat_id="123",
                allowed_chat_ids=("123",),
                interval_seconds=900,
                startup_report=False,
                request_timeout_seconds=20,
                get_updates_timeout_seconds=30,
                max_containers=5,
                telegram_api_base="https://api.telegram.org",
                alerts_enabled=True,
            ),
            telegram=FakeTelegram(),
            collector_client=collector,
            stderr=io.StringIO(),
        )

        service.initialize_alert_cursor()

        self.assertEqual(service._last_alert_seq, 1)
        self.assertEqual(collector.alert_requests, [(None, 1)])

    def test_run_marks_heartbeat_before_entering_poll_loop(self) -> None:
        heartbeat = FakeHeartbeat()
        service = RelayService(
            self.config,
            telegram=InterruptingTelegram(),
            collector_client=FakeCollectorClient(self.snapshot, events_payload=self.events),
            heartbeat=heartbeat,
            stderr=io.StringIO(),
        )

        with self.assertRaises(KeyboardInterrupt):
            service.run()

        self.assertGreaterEqual(heartbeat.calls, 1)

    def test_scheduled_only_mode_caps_sleep_to_keep_heartbeat_fresh(self) -> None:
        heartbeat = FakeHeartbeat()
        config = RelayConfig(
            bot_token="token",
            collector_url="http://collector/snapshot",
            mode="scheduled",
            chat_id="123",
            allowed_chat_ids=("123",),
            interval_seconds=86400,
            startup_report=False,
            request_timeout_seconds=20,
            get_updates_timeout_seconds=30,
            max_containers=5,
            telegram_api_base="https://api.telegram.org",
            health_stale_seconds=120,
        )
        sleeps: list[float] = []

        def stop_after_first_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            raise KeyboardInterrupt

        service = RelayService(
            config,
            telegram=FakeTelegram(),
            collector_client=FakeCollectorClient(self.snapshot, events_payload=self.events),
            heartbeat=heartbeat,
            sleep_fn=stop_after_first_sleep,
            stderr=io.StringIO(),
        )

        with self.assertRaises(KeyboardInterrupt):
            service.run()

        self.assertEqual(sleeps, [60.0])
        self.assertGreaterEqual(heartbeat.calls, 2)


class TelegramClientTests(unittest.TestCase):
    def test_get_updates_uses_timeout_longer_than_long_poll(self) -> None:
        observed: dict[str, object] = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self) -> bytes:
                return b'{"ok": true, "result": []}'

        def fake_urlopen(req, timeout):  # noqa: ANN001
            observed["url"] = req.full_url
            observed["timeout"] = timeout
            return FakeResponse()

        client = TelegramClient(token="token", timeout_seconds=20)
        with patch("tg_box_reporter.telegram_api.request.urlopen", side_effect=fake_urlopen):
            result = client.get_updates(timeout=30)

        self.assertEqual(result, [])
        self.assertEqual(observed["timeout"], 35)


class RelayHeartbeatTests(unittest.TestCase):
    def test_heartbeat_reports_missing_and_stale_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "relay.heartbeat"
            heartbeat = RelayHeartbeat(str(path), clock=lambda: 100.0)

            self.assertEqual(heartbeat.is_healthy(stale_seconds=10), False)

            heartbeat.mark_alive()
            os.utime(path, (95.0, 95.0))
            self.assertEqual(heartbeat.is_healthy(stale_seconds=10), True)
            self.assertEqual(heartbeat.is_healthy(stale_seconds=4), False)

    def test_healthcheck_main_reads_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            path = Path(tmp_dir) / "relay.heartbeat"
            path.touch()
            with patch.dict(
                "os.environ",
                {
                    "RELAY_HEARTBEAT_PATH": str(path),
                    "RELAY_HEALTH_STALE_SECONDS": "60",
                },
                clear=False,
            ):
                self.assertEqual(healthcheck_main(), 0)


class RelayConfigTests(unittest.TestCase):
    def test_from_env_rejects_stale_window_below_long_poll_budget(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "TG_BOT_TOKEN": "token",
                "RELAY_MODE": "polling",
                "RELAY_REQUEST_TIMEOUT_SECONDS": "20",
                "TG_GET_UPDATES_TIMEOUT_SECONDS": "30",
                "RELAY_HEALTH_STALE_SECONDS": "30",
            },
            clear=True,
        ):
            with self.assertRaises(ConfigError) as error:
                RelayConfig.from_env()

        self.assertIn("RELAY_HEALTH_STALE_SECONDS must be >=", str(error.exception))

    def test_from_env_requires_chat_id_for_alert_delivery(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "TG_BOT_TOKEN": "token",
                "RELAY_MODE": "polling",
                "RELAY_ALERTS_ENABLED": "1",
            },
            clear=True,
        ):
            with self.assertRaises(ConfigError) as error:
                RelayConfig.from_env()

        self.assertIn("TG_CHAT_ID must be set", str(error.exception))


class CollectorClientTests(unittest.TestCase):
    def test_projection_url_preserves_path_prefix(self) -> None:
        client = CollectorClient(snapshot_url="http://collector:9707/reporter/snapshot", timeout_seconds=5)

        self.assertEqual(client._projection_url("/events"), "http://collector:9707/reporter/events")
        self.assertEqual(
            client._projection_url("/alerts", query={"after": "1", "limit": "10"}),
            "http://collector:9707/reporter/alerts?after=1&limit=10",
        )


if __name__ == "__main__":
    unittest.main()
