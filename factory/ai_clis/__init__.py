"""AI CLI adapter system — per-dev credential isolation for factory subprocesses.

Adapters mirror the factory/notifications/ pattern: each AI CLI has an
adapter class registered in `default_registry`. The factory orchestrator
and the `devbrain login` / `devbrain logins` commands all dispatch through
the registry.

Usage:
    from ai_clis import default_registry
    adapter_cls = default_registry.get("claude")
    adapter = adapter_cls()
    spawn = adapter.spawn_args(dev, profile_dir)
"""

from ai_clis.base import (
    AdapterRegistry,
    AICliAdapter,
    LoginResult,
    SpawnArgs,
    default_registry,
)

# Trigger adapter self-registration via import side effects.
from ai_clis import claude as _claude  # noqa: F401
from ai_clis import codex as _codex  # noqa: F401
from ai_clis import gemini as _gemini  # noqa: F401

__all__ = [
    "AdapterRegistry",
    "AICliAdapter",
    "LoginResult",
    "SpawnArgs",
    "default_registry",
]
