"""Shared helpers for AI CLI adapters.

Functions here are used across adapters: tunnel pre-flight checks,
listener detection, etc.
"""

from __future__ import annotations

import logging
import socket

logger = logging.getLogger(__name__)


def listener_on_port(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    """Return True iff something is listening on host:port (e.g. via SSH -L)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def verify_reverse_tunnel(port: int) -> bool:
    """Alias for listener_on_port for adapter pre-flight clarity.

    In Model 1 (devs SSH into shared Mac Studio), reverse tunnels expose
    the dev's laptop services back to the Mac Studio. An adapter that
    needs the dev's local PKRelay broker reachable, for example, calls
    this with the tunneled port to verify the tunnel is up before
    invoking the CLI.
    """
    return listener_on_port(port)


def git_author_env(dev) -> dict[str, str]:
    """Build the git authorship env vars for the spawned subprocess.

    Adapters spread these into their `SpawnArgs.env` so that any git
    operations the spawned AI CLI performs (commit, etc.) are
    attributed to the submitting dev rather than the host macOS user.

    Falls back to the dev_id when full_name/email are missing.
    """
    name = getattr(dev, "full_name", None) or getattr(dev, "dev_id", "")
    email = getattr(dev, "email", None) or f"{getattr(dev, 'dev_id', 'dev')}@devbrain.local"
    return {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name,
        "GIT_COMMITTER_EMAIL": email,
    }
