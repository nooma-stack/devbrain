"""Notification router.

Fans notification events out to the recipient's configured channels.
Channels are instantiated from config via the default_registry, and each
dev specifies which channels they want via `devbrain.devs.channels`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from state_machine import FactoryDB
from notifications.base import default_registry, NotificationChannel

# Import all channels so they register with the registry
import notifications.channels.tmux  # noqa: F401
import notifications.channels.smtp  # noqa: F401
import notifications.channels.gmail_dwd  # noqa: F401
import notifications.channels.gchat_dwd  # noqa: F401
import notifications.channels.telegram_bot  # noqa: F401
import notifications.channels.webhook_slack  # noqa: F401
import notifications.channels.webhook_discord  # noqa: F401
import notifications.channels.webhook_generic  # noqa: F401

logger = logging.getLogger(__name__)


@dataclass
class NotificationEvent:
    event_type: str
    recipient_dev_id: str
    title: str
    body: str
    job_id: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class RouterResult:
    skipped: bool = False
    channels_attempted: list[str] = field(default_factory=list)
    channels_delivered: list[str] = field(default_factory=list)
    errors: dict = field(default_factory=dict)


class NotificationRouter:
    def __init__(self, db: FactoryDB, config: dict | None = None):
        self.db = db
        self.config = config if config is not None else self._load_config()
        self._channel_cache: dict[str, NotificationChannel] = {}

    @staticmethod
    def _load_config() -> dict:
        config_path = Path(__file__).parent.parent.parent / "config" / "devbrain.yaml"
        if not config_path.exists():
            return {}
        with open(config_path) as f:
            full_config = yaml.safe_load(f) or {}
        return full_config.get("notifications", {})

    def _get_channel(self, channel_type: str) -> NotificationChannel | None:
        """Get (and cache) a channel instance for the given type."""
        if channel_type in self._channel_cache:
            return self._channel_cache[channel_type]

        channel_config = self.config.get("channels", {}).get(channel_type, {})
        if not channel_config.get("enabled", False):
            return None

        # Strip the "enabled" key before passing to constructor
        init_config = {k: v for k, v in channel_config.items() if k != "enabled"}
        instance = default_registry.instantiate(channel_type, **init_config)
        if instance is None:
            logger.warning("Unknown channel type: %s", channel_type)
            return None

        if not instance.is_configured():
            logger.debug("Channel %s enabled but not configured", channel_type)
            return None

        self._channel_cache[channel_type] = instance
        return instance

    def send(self, event: NotificationEvent) -> RouterResult:
        """Send a notification event to a single recipient."""
        # Global event filter
        notify_events = self.config.get("notify_events", [])
        if notify_events and event.event_type not in notify_events:
            return RouterResult(skipped=True)

        dev = self.db.get_dev(event.recipient_dev_id)
        if not dev:
            logger.warning("Dev %s not registered", event.recipient_dev_id)
            self.db.record_notification(
                recipient_dev_id=event.recipient_dev_id,
                event_type=event.event_type,
                title=event.title,
                body=event.body,
                job_id=event.job_id,
                delivery_errors={"router": "dev not registered"},
                metadata=event.metadata,
            )
            return RouterResult()

        # Per-dev event subscription filter
        dev_events = dev.get("event_subscriptions") or []
        if dev_events and event.event_type not in dev_events:
            return RouterResult(skipped=True)

        attempted: list[str] = []
        delivered: list[str] = []
        errors: dict = {}

        # Iterate the dev's registered channels
        for dev_channel in dev.get("channels", []):
            channel_type = dev_channel.get("type")
            address = dev_channel.get("address")
            if not channel_type or not address:
                continue

            channel = self._get_channel(channel_type)
            if channel is None:
                continue

            attempted.append(channel_type)
            try:
                result = channel.send(
                    address=address,
                    title=event.title,
                    body=event.body,
                    event_type=event.event_type,
                )
            except TypeError:
                result = channel.send(address=address, title=event.title, body=event.body)

            if result.delivered:
                delivered.append(channel_type)
            else:
                errors[channel_type] = result.error or "unknown"

        self.db.record_notification(
            recipient_dev_id=event.recipient_dev_id,
            event_type=event.event_type,
            title=event.title,
            body=event.body,
            job_id=event.job_id,
            channels_attempted=attempted,
            channels_delivered=delivered,
            delivery_errors=errors,
            metadata=event.metadata,
        )

        return RouterResult(
            channels_attempted=attempted,
            channels_delivered=delivered,
            errors=errors,
        )

    def send_multi(self, event: NotificationEvent) -> list[RouterResult]:
        """For events that notify multiple devs (e.g., lock conflicts)."""
        results = [self.send(event)]

        if event.event_type == "lock_conflict":
            blocking_dev_id = event.metadata.get("blocking_dev_id")
            if blocking_dev_id and blocking_dev_id != event.recipient_dev_id:
                blocker_event = NotificationEvent(
                    event_type="lock_conflict",
                    recipient_dev_id=blocking_dev_id,
                    title=f"Your job is blocking {event.recipient_dev_id}",
                    body=(
                        f"Your factory job is holding file locks that another dev's job needs.\n\n"
                        f"{event.body}"
                    ),
                    job_id=event.job_id,
                    metadata=event.metadata,
                )
                results.append(self.send(blocker_event))

        return results
