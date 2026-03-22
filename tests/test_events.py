from __future__ import annotations

import unittest

from tg_box_reporter.events import EventStore, EventValidationError, normalize_event


class EventValidationTests(unittest.TestCase):
    def test_normalize_event_sets_defaults_and_uppercases_method(self) -> None:
        event = normalize_event(
            {
                "source": "vote-mcp",
                "env": "prod",
                "kind": "http.request",
                "name": "polls_hit",
                "method": "get",
            },
            now_utc=lambda: "2026-03-21T00:00:00Z",
        )

        self.assertEqual(event["method"], "GET")
        self.assertEqual(event["ts"], "2026-03-21T00:00:00Z")
        self.assertEqual(event["received_at_utc"], "2026-03-21T00:00:00Z")

    def test_normalize_event_rejects_nested_labels(self) -> None:
        with self.assertRaises(EventValidationError):
            normalize_event(
                {
                    "source": "vote-mcp",
                    "env": "prod",
                    "kind": "http.request",
                    "name": "polls_hit",
                    "labels": {"bad": {"nested": True}},
                }
            )


class EventStoreTests(unittest.TestCase):
    def test_event_store_prunes_by_retention_and_count(self) -> None:
        current = [0.0]

        def clock() -> float:
            return current[0]

        def now_utc() -> str:
            return f"2026-03-21T00:00:{int(current[0]):02d}Z"

        store = EventStore(max_recent=2, retention_seconds=10, clock=clock, now_utc=now_utc)
        store.ingest({"source": "vote-mcp", "env": "prod", "kind": "http.request", "name": "polls_hit"})
        current[0] = 5.0
        store.ingest({"source": "vote-mcp", "env": "prod", "kind": "http.request", "name": "polls_hit"})
        current[0] = 20.0
        store.ingest({"source": "vote-mcp", "env": "demo", "kind": "http.request", "name": "polls_hit"})

        payload = store.snapshot()

        self.assertEqual(payload["received_total"], 3)
        self.assertEqual(payload["retained_total"], 1)
        self.assertEqual(payload["recent"][0]["env"], "demo")

    def test_event_store_summary_groups_similar_events(self) -> None:
        store = EventStore(max_recent=10, retention_seconds=3600, clock=lambda: 0.0, now_utc=lambda: "2026-03-21T00:00:00Z")
        store.ingest(
            {
                "source": "vote-mcp",
                "env": "prod",
                "kind": "http.request",
                "name": "polls_hit",
                "route": "/polls",
                "method": "GET",
            }
        )
        store.ingest(
            {
                "source": "vote-mcp",
                "env": "prod",
                "kind": "http.request",
                "name": "polls_hit",
                "route": "/polls",
                "method": "GET",
            }
        )

        payload = store.snapshot()

        self.assertEqual(payload["summary"][0]["count"], 2)
        self.assertEqual(payload["summary"][0]["route"], "/polls")


if __name__ == "__main__":
    unittest.main()
