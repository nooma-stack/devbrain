"""Gemini CLI adapter.

Gemini, like Claude, has no documented config-dir env var. We swap HOME
for the spawned subprocess to redirect `~/.gemini/` to the per-dev
profile. The swap is constrained to the single subprocess invocation.

Auth strategies:
1. **API key (preferred for headless / SSH sessions).** If the dev has
   set `dev.gemini_api_key` (carried on the Dev model or supplied via
   env at registration), the adapter sets `GEMINI_API_KEY` on the
   spawned env and skips OAuth entirely. No `~/.gemini/` interaction
   needed.
2. **OAuth via Google login.** Default flow when no API key is
   configured. Runs `gemini` (which prompts auth method choice) under
   the swapped HOME. OAuth callback specifics were not exhaustively
   probed; if the flow proves to use a localhost port, the dev should
   set an API key instead (preferred) or coordinate a tunnel manually.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from ai_clis.auth_helpers import git_author_env
from ai_clis.base import AICliAdapter, LoginResult, SpawnArgs

logger = logging.getLogger(__name__)


def _dev_api_key(dev) -> str | None:
    """Return the dev's gemini API key if set, else None."""
    return getattr(dev, "gemini_api_key", None) or None


class GeminiAdapter(AICliAdapter):
    name = "gemini"
    oauth_callback_ports = []  # OAuth specifics unverified; API key path bypasses entirely

    def spawn_args(self, dev, profile_dir: Path) -> SpawnArgs:
        gitconfig = str(profile_dir / ".gitconfig")
        env: dict[str, str] = {
            "HOME": str(profile_dir),
            "GIT_CONFIG_GLOBAL": gitconfig,
            **git_author_env(dev),
        }
        api_key = _dev_api_key(dev)
        if api_key:
            env["GEMINI_API_KEY"] = api_key
        return SpawnArgs(env=env, argv_prefix=["gemini"])

    def login(self, dev, profile_dir: Path) -> LoginResult:
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / ".gemini").mkdir(exist_ok=True)

        api_key = _dev_api_key(dev)
        if api_key:
            return LoginResult(
                success=True,
                hint="Using GEMINI_API_KEY from dev record; OAuth flow skipped.",
            )

        env = {**os.environ, "HOME": str(profile_dir)}
        try:
            result = subprocess.run(
                ["gemini"],
                env=env,
                check=False,
            )
        except FileNotFoundError:
            return LoginResult(
                success=False,
                error="gemini CLI not found on PATH",
                hint="Install Gemini CLI: https://github.com/google-gemini/gemini-cli",
            )

        if result.returncode != 0:
            return LoginResult(
                success=False,
                error=f"gemini exited with code {result.returncode}",
                hint="If OAuth flow needs a localhost callback, set GEMINI_API_KEY instead via the dev record.",
            )

        if not self.is_logged_in(dev, profile_dir):
            return LoginResult(
                success=False,
                error="gemini exited but ~/.gemini/google_accounts.json not found",
                hint=f"Check {profile_dir}/.gemini/google_accounts.json or set GEMINI_API_KEY.",
            )

        return LoginResult(success=True)

    def is_logged_in(self, dev, profile_dir: Path) -> bool:
        if _dev_api_key(dev):
            return True
        return (profile_dir / ".gemini" / "google_accounts.json").exists()

    def required_dotfiles(self) -> list[str]:
        return [".gemini/", ".gitconfig"]


default_register = True
if default_register:
    from ai_clis.base import default_registry

    default_registry.register(GeminiAdapter)
