from __future__ import annotations

import json
import threading
import unittest
from urllib import request
from urllib.error import HTTPError

from tg_box_reporter.collector import CollectorHTTPServer, SnapshotCache
from tg_box_reporter.events import EventStore


class FakeCollector:
    def __init__(self, snapshot: dict[str, object]) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def collect(self) -> dict[str, object]:
        self.calls += 1
        return self.snapshot


class BrokenCollector:
    def collect(self) -> dict[str, object]:
        raise RuntimeError("snapshot failed")


class CollectorHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = {
            "generated_at_utc": "2026-03-21T00:00:00Z",
            "status": "warning",
            "problem_summary": {"total": 1, "critical": 0, "warning": 1, "info": 0},
            "host": {
                "hostname": "box-1",
                "cpu_count": 4,
                "uptime_seconds": 60,
                "load_1m": 0.1,
                "load_5m": 0.2,
                "load_15m": 0.3,
                "memory": {"used_percent": 50.0},
                "swap": {"used_percent": 0.0},
                "disk": {"path": "/", "used_percent": 20.0},
            },
            "docker": {
                "available": True,
                "source": "docker-cli",
                "summary": {"total": 2, "running": 1, "restarting": 0, "unhealthy": 1, "exited": 1},
                "containers": [
                    {"name": "api", "cpu_percent": 10.0, "mem_percent": 20.0, "restart_count": 0, "status": "running", "health": "healthy"},
                    {"name": "worker", "cpu_percent": 30.0, "mem_percent": 40.0, "restart_count": 2, "status": "exited", "health": ""},
                ],
            },
            "errors": [],
            "problems": [
                {
                    "severity": "warning",
                    "source": "container:worker",
                    "code": "container_exited",
                    "detail": "container worker is exited",
                }
            ],
        }
        self.collector = FakeCollector(self.snapshot)
        self.cache = SnapshotCache(self.collector, cache_seconds=5)
        self.server = CollectorHTTPServer(
            ("127.0.0.1", 0),
            self.cache,
            event_store=EventStore(max_recent=50, retention_seconds=3600),
            event_token="secret-token",
            event_max_bytes=4096,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def _get_json(self, path: str) -> dict[str, object]:
        with request.urlopen(f"{self.base_url}{path}", timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post_json(self, path: str, payload: dict[str, object], *, token: str = "secret-token") -> tuple[int, dict[str, object]]:
        req = request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_summary_projection_is_compact_and_stable(self) -> None:
        payload = self._get_json("/summary")
        self.assertEqual(payload["status"], "warning")
        self.assertEqual(payload["host"]["hostname"], "box-1")
        self.assertEqual(payload["docker"]["summary"]["total"], 2)
        self.assertNotIn("containers", payload["docker"])

    def test_containers_projection_respects_limit_and_sorting(self) -> None:
        payload = self._get_json("/containers?limit=1")
        self.assertEqual(payload["total"], 2)
        self.assertEqual(len(payload["containers"]), 1)
        self.assertEqual(payload["containers"][0]["name"], "worker")

    def test_problems_projection_returns_only_problem_view(self) -> None:
        payload = self._get_json("/problems")
        self.assertEqual(payload["problem_summary"]["total"], 1)
        self.assertEqual(payload["problems"][0]["code"], "container_exited")
        self.assertNotIn("docker", payload)

    def test_readyz_uses_cached_snapshot_once(self) -> None:
        first = self._get_json("/readyz")
        second = self._get_json("/readyz")
        self.assertEqual(first["ok"], True)
        self.assertEqual(second["status"], "warning")
        self.assertEqual(self.collector.calls, 1)

    def test_readyz_returns_503_when_snapshot_generation_fails(self) -> None:
        cache = SnapshotCache(BrokenCollector(), cache_seconds=0)
        server = CollectorHTTPServer(
            ("127.0.0.1", 0),
            cache,
            event_store=EventStore(max_recent=50, retention_seconds=3600),
            event_token="secret-token",
            event_max_bytes=4096,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"

        try:
            with self.assertRaises(HTTPError) as error:
                request.urlopen(f"{base_url}/readyz", timeout=5)
            self.assertEqual(error.exception.code, 503)
            payload = json.loads(error.exception.read().decode("utf-8"))
            self.assertEqual(payload["ok"], False)
            self.assertIn("snapshot failed", payload["error"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_events_ingest_and_recent_projection(self) -> None:
        status, payload = self._post_json(
            "/events",
            {
                "source": "vote-mcp",
                "env": "prod",
                "kind": "http.request",
                "name": "polls_hit",
                "route": "/polls",
                "method": "GET",
                "status": 200,
            },
        )

        self.assertEqual(status, 202)
        self.assertEqual(payload["event"]["name"], "polls_hit")
        recent = self._get_json("/events/recent?limit=1")
        self.assertEqual(recent["retained_total"], 1)
        self.assertEqual(recent["events"][0]["route"], "/polls")
        summary = self._get_json("/events/summary?groups=1")
        self.assertEqual(summary["groups"][0]["count"], 1)
        self.assertEqual(summary["groups"][0]["source"], "vote-mcp")

    def test_events_post_requires_matching_bearer_token(self) -> None:
        req = request.Request(
            f"{self.base_url}/events",
            data=b'{"source":"vote-mcp","env":"prod","kind":"http.request","name":"polls_hit"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with self.assertRaises(HTTPError) as error:
            request.urlopen(req, timeout=5)

        self.assertEqual(error.exception.code, 401)

    def test_events_post_rejects_invalid_payload(self) -> None:
        req = request.Request(
            f"{self.base_url}/events",
            data=b'{"source":"vote-mcp"}',
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer secret-token",
            },
            method="POST",
        )

        with self.assertRaises(HTTPError) as error:
            request.urlopen(req, timeout=5)

        self.assertEqual(error.exception.code, 400)
        payload = json.loads(error.exception.read().decode("utf-8"))
        self.assertIn("env", payload["error"])


if __name__ == "__main__":
    unittest.main()
