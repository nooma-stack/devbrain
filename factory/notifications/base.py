"""Base classes for the DevBrain notification channel system.

Channels implement NotificationChannel and are registered in a ChannelRegistry.
The router uses the registry to instantiate channels from config.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Type

logger = logging.getLogger(__name__)


@dataclass
class ChannelResult:
    """Result of a notification delivery attempt."""
    delivered: bool
    channel: str
    error: str | None = None
    metadata: dict = field(default_factory=dict)


class NotificationChannel(ABC):
    """Base class for notification delivery channels."""

    name: str = ""

    def __init__(self, **config):
        self.config = config

    @abstractmethod
    def is_configured(self) -> bool:
        """Return True if the channel has enough config to actually send."""
        ...

    @abstractmethod
    def send(self, address: str, title: str, body: str) -> ChannelResult:
        """Deliver a notification to the given address.

        Address format is channel-specific:
          - tmux: session name
          - smtp/gmail_dwd: email address
          - gchat_dwd: email address or space ID
          - telegram_bot: chat_id
          - webhook_*: URL
        """
        ...


class ChannelRegistry:
    """Registry mapping channel type names to channel classes."""

    def __init__(self):
        self._channels: dict[str, Type[NotificationChannel]] = {}

    def register(self, name: str, channel_class: Type[NotificationChannel]) -> None:
        self._channels[name] = channel_class
        logger.debug("Registered channel: %s → %s", name, channel_class.__name__)

    def get(self, name: str) -> Type[NotificationChannel] | None:
        return self._channels.get(name)

    def list_types(self) -> list[str]:
        return list(self._channels.keys())

    def instantiate(self, name: str, **config) -> NotificationChannel | None:
        cls = self.get(name)
        if cls is None:
            return None
        return cls(**config)


# Global registry — channels register themselves on import
default_registry = ChannelRegistry()
