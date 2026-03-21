from __future__ import annotations

import json
from urllib import request


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, *, token: str, timeout_seconds: int, api_base: str = "https://api.telegram.org"):
        self.timeout_seconds = timeout_seconds
        self.base_url = f"{api_base.rstrip('/')}/bot{token}"

    def send_message(self, *, chat_id: str, text: str) -> dict[str, object]:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        return self._post_json("sendMessage", payload)

    def get_updates(self, *, offset: int | None = None, timeout: int = 30) -> list[dict[str, object]]:
        payload: dict[str, object] = {
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        response = self._post_json(
            "getUpdates",
            payload,
            timeout_seconds=max(self.timeout_seconds, timeout + 5),
        )
        result = response.get("result")
        if not isinstance(result, list):
            raise TelegramError("Telegram getUpdates response did not include a result list")
        return [item for item in result if isinstance(item, dict)]

    def _post_json(
        self,
        method: str,
        payload: dict[str, object],
        *,
        timeout_seconds: int | None = None,
    ) -> dict[str, object]:
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resolved_timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        with request.urlopen(req, timeout=resolved_timeout) as response:
            parsed = json.loads(response.read().decode("utf-8"))
        if not parsed.get("ok", False):
            raise TelegramError(str(parsed))
        return parsed
