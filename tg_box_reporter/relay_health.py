from __future__ import annotations

import os
import time
from pathlib import Path

from .config import DEFAULT_RELAY_HEALTH_STALE_SECONDS, DEFAULT_RELAY_HEARTBEAT_PATH


class RelayHeartbeat:
    def __init__(self, path: str, *, clock: callable = time.time):
        self.path = Path(path)
        self.clock = clock

    def mark_alive(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()

    def is_healthy(self, *, stale_seconds: int | float) -> bool:
        try:
            modified_at = self.path.stat().st_mtime
        except FileNotFoundError:
            return False
        return self.clock() - modified_at <= stale_seconds


def healthcheck_main() -> int:
    path = os.environ.get("RELAY_HEARTBEAT_PATH", DEFAULT_RELAY_HEARTBEAT_PATH).strip()
    if not path:
        path = DEFAULT_RELAY_HEARTBEAT_PATH

    raw_stale_seconds = os.environ.get(
        "RELAY_HEALTH_STALE_SECONDS",
        str(DEFAULT_RELAY_HEALTH_STALE_SECONDS),
    ).strip()
    try:
        stale_seconds = max(1.0, float(raw_stale_seconds))
    except ValueError:
        return 1

    heartbeat = RelayHeartbeat(path)
    return 0 if heartbeat.is_healthy(stale_seconds=stale_seconds) else 1


if __name__ == "__main__":
    raise SystemExit(healthcheck_main())
