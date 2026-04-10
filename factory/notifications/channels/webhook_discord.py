"""Discord webhook notification channel."""

from __future__ import annotations

from notifications.base import NotificationChannel, ChannelResult, default_registry
from notifications.channels._webhook_base import post_webhook


class WebhookDiscordChannel(NotificationChannel):
    name = "webhook_discord"

    def is_configured(self) -> bool:
        return True

    def send(self, address: str, title: str, body: str, **kwargs) -> ChannelResult:
        content = f"🔔 **{title}**\n\n{body}"
        return post_webhook(address, {"content": content}, self.name)


default_registry.register("webhook_discord", WebhookDiscordChannel)
