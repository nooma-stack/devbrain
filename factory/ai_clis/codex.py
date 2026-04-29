"""Codex CLI adapter.

Codex supports the `CODEX_HOME` env var to redirect its profile dir,
verified via behavioral probe (codex emits a startup warning when
CODEX_HOME points at a missing directory). We use this for precise
per-dev credential isolation — no HOME swap needed for Codex.

For login, we use `codex login --device-auth` which avoids binding a
localhost callback port. Devs SSH'd into the shared Mac Studio can
complete OAuth without an extra reverse tunnel: read URL, paste in
laptop browser, get a code, type it back into the SSH session.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from ai_clis.auth_helpers import git_author_env
from ai_clis.base import AICliAdapter, LoginResult, SpawnArgs

logger = logging.getLogger(__name__)


class CodexAdapter(AICliAdapter):
    name = "codex"
    oauth_callback_ports = []  # device-auth flow needs no callback port

    def spawn_args(self, dev, profile_dir: Path) -> SpawnArgs:
        codex_home = str(profile_dir / ".codex")
        gitconfig = str(profile_dir / ".gitconfig")
        env = {
            "CODEX_HOME": codex_home,
            "GIT_CONFIG_GLOBAL": gitconfig,
            **git_author_env(dev),
        }
        return SpawnArgs(env=env, argv_prefix=["codex"])

    def login(self, dev, profile_dir: Path) -> LoginResult:
        codex_home = profile_dir / ".codex"
        codex_home.mkdir(parents=True, exist_ok=True)

        env = {**os.environ, "CODEX_HOME": str(codex_home)}
        try:
            result = subprocess.run(
                ["codex", "login", "--device-auth"],
                env=env,
                check=False,
            )
        except FileNotFoundError:
            return LoginResult(
                success=False,
                error="codex CLI not found on PATH",
                hint="Install Codex: https://github.com/openai/codex",
            )

        if result.returncode != 0:
            return LoginResult(
                success=False,
                error=f"codex login exited with code {result.returncode}",
                hint="Re-run `devbrain login --dev <id> --cli codex` and complete the device-code flow.",
            )

        if not self.is_logged_in(dev, profile_dir):
            return LoginResult(
                success=False,
                error="codex login completed but auth.json not found",
                hint=f"Check {codex_home}/auth.json was written.",
            )

        return LoginResult(success=True)

    def is_logged_in(self, dev, profile_dir: Path) -> bool:
        return (profile_dir / ".codex" / "auth.json").exists()

    def required_dotfiles(self) -> list[str]:
        return [".codex/auth.json", ".gitconfig"]


default_register = True
if default_register:
    from ai_clis.base import default_registry

    default_registry.register(CodexAdapter)
