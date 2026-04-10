"""Slack incoming webhook notification channel."""

from __future__ import annotations

from notifications.base import NotificationChannel, ChannelResult, default_registry
from notifications.channels._webhook_base import post_webhook


class WebhookSlackChannel(NotificationChannel):
    name = "webhook_slack"

    def is_configured(self) -> bool:
        return True  # URL per-dev, no global config

    def send(self, address: str, title: str, body: str, **kwargs) -> ChannelResult:
        text = f"🔔 *{title}*\n\n{body}"
        return post_webhook(address, {"text": text}, self.name)


default_registry.register("webhook_slack", WebhookSlackChannel)
