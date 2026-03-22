from __future__ import annotations

import json
import posixpath
import sys
import time
from urllib import request
from urllib.parse import urlsplit, urlunsplit

from .config import RelayConfig
from .formatting import format_containers, format_events, format_problems, format_report, format_summary, split_message
from .relay_health import RelayHeartbeat
from .telegram_api import TelegramClient

COMMAND_REPORT_TYPES = {
    "/report": "report",
    "/summary": "summary",
    "/containers": "containers",
    "/problems": "problems",
    "/events": "events",
}
HELP_TEXT = "\n".join([*COMMAND_REPORT_TYPES, "/help"])


class CollectorClient:
    def __init__(self, *, snapshot_url: str, timeout_seconds: int):
        self.snapshot_url = snapshot_url
        self.timeout_seconds = timeout_seconds

    def fetch_snapshot(self) -> dict[str, object]:
        return self._fetch(self.snapshot_url)

    def fetch_events(self) -> dict[str, object]:
        return self._fetch(self._projection_url("/events"))

    def _fetch(self, url: str) -> dict[str, object]:
        with request.urlopen(url, timeout=self.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def _projection_url(self, path: str) -> str:
        parsed = urlsplit(self.snapshot_url)
        current_path = parsed.path or "/snapshot"
        base_dir = posixpath.dirname(current_path.rstrip("/"))
        normalized_path = posixpath.join(base_dir or "/", path.lstrip("/"))
        return urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))


class RelayService:
    def __init__(
        self,
        config: RelayConfig,
        *,
        telegram: TelegramClient,
        collector_client: CollectorClient,
        heartbeat: RelayHeartbeat | None = None,
        clock: callable = time.monotonic,
        sleep_fn: callable = time.sleep,
        stderr = sys.stderr,
    ):
        self.config = config
        self.telegram = telegram
        self.collector_client = collector_client
        self.heartbeat = heartbeat or RelayHeartbeat(config.heartbeat_path)
        self.clock = clock
        self.sleep_fn = sleep_fn
        self.stderr = stderr

    def run(self) -> int:
        next_report_at = self.clock()
        if not self.config.startup_report:
            next_report_at += self.config.interval_seconds
        offset: int | None = None

        self._mark_alive()
        while True:
            self._mark_alive()
            now = self.clock()
            if self._scheduled_enabled() and now >= next_report_at:
                self._safe_send_report(self.config.chat_id, "report")
                next_report_at = self.clock() + self.config.interval_seconds

            if self._polling_enabled():
                poll_timeout = self.config.get_updates_timeout_seconds
                if self._scheduled_enabled():
                    remaining = max(1, int(next_report_at - self.clock()))
                    poll_timeout = min(poll_timeout, remaining)
                try:
                    updates = self.telegram.get_updates(offset=offset, timeout=poll_timeout)
                except Exception as exc:
                    self.stderr.write(f"relay poll error: {exc}\n")
                    self.stderr.flush()
                    self._mark_alive()
                    self.sleep_fn(1.0)
                    continue
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = update_id + 1
                    self.handle_update(update)
                self._mark_alive()
                continue

            self._mark_alive()
            sleep_for = max(1.0, next_report_at - self.clock())
            max_sleep = max(1.0, float(self.config.health_stale_seconds) / 2.0)
            self.sleep_fn(min(sleep_for, max_sleep))

    def handle_update(self, update: dict[str, object]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return
        chat_id = str(chat.get("id") or "").strip()
        text = str(message.get("text") or "").strip()
        if not chat_id or not text.startswith("/"):
            return
        self.handle_command(chat_id, text)

    def handle_command(self, chat_id: str, text: str) -> None:
        command = text.split()[0].split("@")[0]
        if not self._chat_allowed(chat_id):
            self.telegram.send_message(chat_id=chat_id, text="unauthorized chat")
            return

        report_type = COMMAND_REPORT_TYPES.get(command)
        if report_type is not None:
            self._send_report_for_command(chat_id, report_type)
            return
        if command == "/help":
            self.telegram.send_message(chat_id=chat_id, text=HELP_TEXT)
            return
        self.telegram.send_message(chat_id=chat_id, text="unknown command; try /help")

    def _send_report_for_command(self, chat_id: str, report_type: str) -> None:
        try:
            self.send_report(chat_id, report_type)
        except Exception as exc:
            self.telegram.send_message(chat_id=chat_id, text=f"report failed: {exc}")

    def _safe_send_report(self, chat_id: str, report_type: str) -> None:
        try:
            self.send_report(chat_id, report_type)
        except Exception as exc:
            self.stderr.write(f"relay error: {exc}\n")
            self.stderr.flush()

    def send_report(self, chat_id: str, report_type: str) -> None:
        text = self._render_report(report_type)
        for chunk in split_message(text):
            self.telegram.send_message(chat_id=chat_id, text=chunk)

    def _render_report(self, report_type: str) -> str:
        if report_type == "report":
            snapshot = self.collector_client.fetch_snapshot()
            return format_report(snapshot, max_containers=self.config.max_containers)
        if report_type == "summary":
            snapshot = self.collector_client.fetch_snapshot()
            return format_summary(snapshot)
        if report_type == "containers":
            snapshot = self.collector_client.fetch_snapshot()
            return format_containers(snapshot)
        if report_type == "problems":
            snapshot = self.collector_client.fetch_snapshot()
            return format_problems(snapshot)
        if report_type == "events":
            return format_events(self.collector_client.fetch_events())
        raise ValueError(f"unknown report type: {report_type}")

    def _scheduled_enabled(self) -> bool:
        return self.config.mode in {"scheduled", "hybrid"}

    def _polling_enabled(self) -> bool:
        return self.config.mode in {"polling", "hybrid"}

    def _chat_allowed(self, chat_id: str) -> bool:
        if not self.config.allowed_chat_ids:
            return True
        return chat_id in self.config.allowed_chat_ids

    def _mark_alive(self) -> None:
        try:
            self.heartbeat.mark_alive()
        except Exception as exc:
            self.stderr.write(f"relay heartbeat error: {exc}\n")
            self.stderr.flush()


def main() -> int:
    config = RelayConfig.from_env()
    service = RelayService(
        config,
        telegram=TelegramClient(
            token=config.bot_token,
            timeout_seconds=config.request_timeout_seconds,
            api_base=config.telegram_api_base,
        ),
        collector_client=CollectorClient(
            snapshot_url=config.collector_url,
            timeout_seconds=config.request_timeout_seconds,
        ),
    )
    try:
        return service.run()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
