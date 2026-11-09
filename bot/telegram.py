"""Minimal Telegram Bot API client (just what this bot needs)."""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)
MAX_LEN = 4096


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str, chat_id: str, thread_id: int | None, dry_run: bool = False):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.dry_run = dry_run
        self._fake_id = 0

    def _call(self, method: str, payload: dict[str, Any], timeout: int = 30) -> Any:
        for attempt in range(3):
            resp = requests.post(f"{self.base}/{method}", json=payload, timeout=timeout)
            data = resp.json()
            if data.get("ok"):
                return data["result"]
            retry = data.get("parameters", {}).get("retry_after")
            if resp.status_code == 429 and retry and attempt < 2:
                time.sleep(int(retry) + 1)
                continue
            # Never log the URL: it contains the bot token.
            raise TelegramError(f"{method} failed: {data.get('description')}")
        raise TelegramError(f"{method} failed after retries")

    def send(self, html: str) -> int:
        """Post an HTML-formatted message to the topic. Returns the message id."""
        if len(html) > MAX_LEN:
            html = html[: MAX_LEN - 1] + "…"
        if self.dry_run:
            self._fake_id += 1
            print(f"\n----- [dry run] message #{self._fake_id} -----\n{html}\n")
            return -self._fake_id
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": html,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if self.thread_id:
            payload["message_thread_id"] = self.thread_id
        msg = self._call("sendMessage", payload)
        time.sleep(1)  # stay well under group rate limits
        return int(msg["message_id"])

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        """Fetch queued updates (reactions). Telegram keeps them ~24h."""
        if self.dry_run:
            return []
        payload: dict[str, Any] = {"timeout": 0, "allowed_updates": ["message_reaction"]}
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", payload)
