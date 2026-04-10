"""Telegram Bot notification channel.

Uses the Telegram Bot API via stdlib urllib (no external dependencies).
Includes a `discover_chat_id` helper that reads recent bot updates to auto-locate
a user's private chat id — useful for first-time setup.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

from notifications.base import ChannelResult, NotificationChannel, default_registry

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramBotChannel(NotificationChannel):
    """Send notifications via a Telegram bot."""

    name = "telegram_bot"

    def __init__(self, bot_token: str = "", bot_username: str = "", **kwargs):
        super().__init__(bot_token=bot_token, bot_username=bot_username, **kwargs)
        self.bot_token = bot_token
        self.bot_username = bot_username

    def _token(self) -> str:
        return self.bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")

    def is_configured(self) -> bool:
        return bool(self._token())

    def _call(self, method: str, params: dict) -> dict:
        """POST to the Telegram Bot API and return the parsed JSON response."""
        url = TELEGRAM_API.format(token=self._token(), method=method)
        data = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
        return json.loads(raw.decode("utf-8"))

    def send(self, address: str, title: str, body: str, **kwargs) -> ChannelResult:
        if not self.is_configured():
            return ChannelResult(
                delivered=False,
                channel=self.name,
                error="telegram_bot not configured (missing bot_token)",
            )

        text = f"🔔 *{title}*\n\n{body}"
        try:
            response = self._call(
                "sendMessage",
                {
                    "chat_id": address,
                    "text": text,
                    "parse_mode": "Markdown",
                },
            )
        except Exception as e:
            logger.error("Telegram send failure: %s", e)
            return ChannelResult(
                delivered=False,
                channel=self.name,
                error=f"{type(e).__name__}: {e}",
            )

        if not response.get("ok"):
            return ChannelResult(
                delivered=False,
                channel=self.name,
                error=response.get("description"),
            )

        return ChannelResult(
            delivered=True,
            channel=self.name,
            metadata={"message_id": response["result"]["message_id"]},
        )

    def discover_chat_id(self, username_hint: str | None = None) -> str | None:
        """Discover a chat_id from recent bot updates.

        Calls getUpdates and returns the most recent private-chat id. If
        `username_hint` is provided, only matches chats where chat.username equals
        the hint. Returns None on error or when no match is found.
        """
        try:
            response = self._call("getUpdates", {"limit": 20})
            if not response.get("ok"):
                logger.warning(
                    "telegram getUpdates failed: %s", response.get("description")
                )
                return None

            updates = response.get("result", []) or []
            matches: list[dict] = []
            for update in updates:
                message = (
                    update.get("message")
                    or update.get("edited_message")
                    or update.get("channel_post")
                    or {}
                )
                chat = message.get("chat") or {}
                if chat.get("type") != "private":
                    continue
                if username_hint is not None and chat.get("username") != username_hint:
                    continue
                matches.append(chat)

            if not matches:
                return None

            # Most recent = last in the list (getUpdates returns chronological order)
            return str(matches[-1]["id"])
        except Exception as e:
            logger.warning("telegram discover_chat_id failed: %s", e)
            return None


default_registry.register("telegram_bot", TelegramBotChannel)
