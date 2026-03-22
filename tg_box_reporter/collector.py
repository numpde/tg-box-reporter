from __future__ import annotations

import json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import CollectorConfig
from .events import EVENT_PROJECTIONS, EventStore, EventValidationError
from .projections import SNAPSHOT_PROJECTIONS
from .snapshot import SnapshotCollector


class SnapshotCache:
    def __init__(self, collector: SnapshotCollector, *, cache_seconds: int):
        self.collector = collector
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._snapshot: dict[str, object] | None = None
        self._loaded_at = 0.0

    def current(self) -> dict[str, object]:
        now = time.monotonic()
        with self._lock:
            if self._snapshot is not None and now - self._loaded_at <= self.cache_seconds:
                return self._snapshot
            snapshot = self.collector.collect()
            self._snapshot = snapshot
            self._loaded_at = now
            return snapshot


class CollectorHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        cache: SnapshotCache,
        *,
        event_store: EventStore,
        event_token: str,
        event_max_bytes: int,
    ):
        super().__init__(server_address, CollectorHandler)
        self.cache = cache
        self.event_store = event_store
        self.event_token = event_token
        self.event_max_bytes = event_max_bytes


class CollectorHandler(BaseHTTPRequestHandler):
    server: CollectorHTTPServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/healthz":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return

        snapshot_projection = SNAPSHOT_PROJECTIONS.get(path)
        if snapshot_projection is not None:
            snapshot = self._load_snapshot()
            if snapshot is None:
                return
            self._send_json(HTTPStatus.OK, snapshot_projection(snapshot, query))
            return

        event_projection = EVENT_PROJECTIONS.get(path)
        if event_projection is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        payload = self.server.event_store.snapshot()
        payload["ingest_enabled"] = bool(self.server.event_token)
        self._send_json(HTTPStatus.OK, event_projection(payload, query))

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/events":
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        if not self.server.event_token:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "event ingestion is disabled"})
            return
        if not self._authorized():
            self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return

        content_length = self.headers.get("Content-Length", "").strip()
        if not content_length:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Content-Length is required"})
            return
        try:
            body_length = int(content_length)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "Content-Length must be an integer"})
            return
        if body_length <= 0:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "request body must not be empty"})
            return
        if body_length > self.server.event_max_bytes:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"ok": False, "error": f"request body exceeds {self.server.event_max_bytes} bytes"},
            )
            return

        raw_body = self.rfile.read(body_length)
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"invalid JSON body: {exc}"})
            return
        try:
            event = self.server.event_store.ingest(payload)
        except EventValidationError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            return
        self._send_json(HTTPStatus.ACCEPTED, {"ok": True, "event": event})

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        _ = (format, args)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "").strip()
        return header == f"Bearer {self.server.event_token}"

    def _load_snapshot(self) -> dict[str, object] | None:
        try:
            return self.server.cache.current()
        except Exception as exc:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(exc)})
            return None

    def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    config = CollectorConfig.from_env()
    collector = SnapshotCollector(config)
    cache = SnapshotCache(collector, cache_seconds=config.cache_seconds)
    event_store = EventStore(
        max_recent=config.event_max_recent,
        retention_seconds=config.event_retention_seconds,
    )
    server = CollectorHTTPServer(
        (config.bind_host, config.port),
        cache,
        event_store=event_store,
        event_token=config.event_token,
        event_max_bytes=config.event_max_bytes,
    )
    print(f"collector listening on http://{config.bind_host}:{config.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
