"""Telegram delivery via the Bot API (plain HTTPS, no extra dependency)."""
from __future__ import annotations

import logging
import time

import requests

from .fmt import chunk_parts, strip_tags

log = logging.getLogger("stockbot.notify")


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _call(self, method: str, payload: dict | None = None, *, timeout: int = 30) -> dict:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        try:
            resp = requests.post(url, json=payload or {}, timeout=timeout)
        except requests.RequestException as exc:
            # Never let the token leak into logs through the exception text.
            raise TelegramError(f"{method}: network error ({type(exc).__name__})") from None
        try:
            data = resp.json()
        except ValueError:
            raise TelegramError(f"{method}: non-JSON response (HTTP {resp.status_code})") from None
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description', resp.status_code)}"
                                f"|retry_after={data.get('parameters', {}).get('retry_after', 0)}")
        return data["result"]

    def whoami(self) -> dict:
        return self._call("getMe")

    def recent_chats(self) -> list[dict]:
        """Chats that have messaged the bot recently (used to discover your chat id)."""
        seen: dict[int, dict] = {}
        for upd in self._call("getUpdates", {"limit": 100}):
            msg = upd.get("message") or upd.get("channel_post") or {}
            chat = msg.get("chat")
            if chat:
                seen[chat["id"]] = chat
        return list(seen.values())

    def send_html(self, text: str) -> None:
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        for attempt in range(3):
            try:
                self._call("sendMessage", payload)
                return
            except TelegramError as exc:
                msg = str(exc)
                if "retry_after=" in msg and not msg.endswith("retry_after=0"):
                    wait = int(msg.rsplit("=", 1)[1])
                    time.sleep(min(wait + 1, 60))
                    continue
                if "can't parse entities" in msg or "Bad Request" in msg:
                    # Formatting problem: resend as plain text rather than lose the content.
                    log.warning("HTML rejected by Telegram, resending as plain text")
                    self._call("sendMessage", {"chat_id": self.chat_id, "text": strip_tags(text)[:4096],
                                               "disable_web_page_preview": True})
                    return
                if attempt == 2:
                    raise
                time.sleep(2)

    def send_parts(self, parts: list[str]) -> int:
        messages = chunk_parts(parts)
        for i, msg in enumerate(messages):
            self.send_html(msg)
            if i < len(messages) - 1:
                time.sleep(1.1)  # Telegram allows ~1 msg/sec per chat
        return len(messages)
