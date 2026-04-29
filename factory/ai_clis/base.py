"""Base classes for the DevBrain AI CLI adapter system.

Adapters implement AICliAdapter and are registered in an AdapterRegistry.
Each adapter encapsulates how its CLI is spawned with per-dev credentials,
how its OAuth login flow works, and how to verify a dev is logged in.

Mirrors factory/notifications/base.py in shape.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Type

logger = logging.getLogger(__name__)


@dataclass
class SpawnArgs:
    """Env overrides + argv prefix returned by adapter.spawn_args()."""

    env: dict[str, str] = field(default_factory=dict)
    argv_prefix: list[str] = field(default_factory=list)


@dataclass
class LoginResult:
    """Result of an adapter.login() call."""

    success: bool
    error: str | None = None
    hint: str | None = None


class AICliAdapter(ABC):
    """Base class for AI CLI per-dev credential adapters."""

    name: ClassVar[str] = ""
    oauth_callback_ports: ClassVar[list[int]] = []

    @abstractmethod
    def spawn_args(self, dev, profile_dir: Path) -> SpawnArgs:
        """Return env overrides + argv prefix for invoking this CLI for `dev`.

        The returned env is merged on top of os.environ by the caller; any
        caller-supplied env_override is merged on top of the adapter's env.

        Implementations pick their own credential-isolation strategy
        (env var override where supported; HOME swap where the CLI lacks
        a config-dir env var). Always include git author env vars
        (GIT_CONFIG_GLOBAL, GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL) to ensure
        per-dev commit attribution.
        """
        ...

    @abstractmethod
    def login(self, dev, profile_dir: Path) -> LoginResult:
        """Run the CLI's native login flow, landing creds in profile_dir."""
        ...

    @abstractmethod
    def is_logged_in(self, dev, profile_dir: Path) -> bool:
        """Return True iff the dev's profile_dir contains valid credentials."""
        ...

    @abstractmethod
    def required_dotfiles(self) -> list[str]:
        """List of relative paths under profile_dir that must exist for the CLI to work."""
        ...


class AdapterRegistry:
    """Registry mapping adapter name → adapter class."""

    def __init__(self) -> None:
        self._adapters: dict[str, Type[AICliAdapter]] = {}

    def register(self, adapter_class: Type[AICliAdapter]) -> None:
        if not adapter_class.name:
            raise ValueError(
                f"{adapter_class.__name__} must define a non-empty `name` class attribute"
            )
        if adapter_class.name in self._adapters:
            raise ValueError(f"adapter {adapter_class.name!r} already registered")
        self._adapters[adapter_class.name] = adapter_class
        logger.debug("Registered AI CLI adapter: %s", adapter_class.name)

    def get(self, name: str) -> Type[AICliAdapter]:
        if name not in self._adapters:
            raise KeyError(f"unknown AI CLI adapter: {name!r}")
        return self._adapters[name]

    def list_names(self) -> list[str]:
        return list(self._adapters.keys())

    def all(self) -> list[Type[AICliAdapter]]:
        return list(self._adapters.values())


default_registry = AdapterRegistry()
