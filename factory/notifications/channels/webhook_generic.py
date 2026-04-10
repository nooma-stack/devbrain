"""Generic webhook channel — sends JSON with title/body/event_type/timestamp."""

from __future__ import annotations

from datetime import datetime, timezone

from notifications.base import NotificationChannel, ChannelResult, default_registry
from notifications.channels._webhook_base import post_webhook


class WebhookGenericChannel(NotificationChannel):
    name = "webhook_generic"

    def is_configured(self) -> bool:
        return True

    def send(self, address: str, title: str, body: str, event_type: str = "", **kwargs) -> ChannelResult:
        payload = {
            "title": title,
            "body": body,
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return post_webhook(address, payload, self.name)


default_registry.register("webhook_generic", WebhookGenericChannel)
