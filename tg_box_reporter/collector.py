from __future__ import annotations

import json
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import CollectorConfig
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
    def __init__(self, server_address: tuple[str, int], cache: SnapshotCache):
        super().__init__(server_address, CollectorHandler)
        self.cache = cache


class CollectorHandler(BaseHTTPRequestHandler):
    server: CollectorHTTPServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/healthz":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return

        projection = SNAPSHOT_PROJECTIONS.get(path)
        if projection is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return

        snapshot = self._load_snapshot()
        if snapshot is None:
            return
        self._send_json(HTTPStatus.OK, projection(snapshot, query))

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        _ = (format, args)

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
    server = CollectorHTTPServer((config.bind_host, config.port), cache)
    print(f"collector listening on http://{config.bind_host}:{config.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
