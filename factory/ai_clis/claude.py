"""Claude Code CLI adapter.

Claude Code does NOT expose a config-dir env var (per official docs at
code.claude.com/docs/en/settings — the only customizable path is
`autoMemoryDirectory` in settings.json). To isolate per-dev creds we
have to swap HOME for the spawned subprocess.

The HOME swap is constrained to the single AI subprocess invocation —
the orchestrator's HOME and broader environment stay untouched. Git
authorship is set explicitly via GIT_CONFIG_GLOBAL + GIT_AUTHOR_* env
vars on top of the HOME swap (these win over .gitconfig discovery).

Login flow: Claude uses a hosted callback at
`platform.claude.com/oauth/code/callback` — no localhost listener, so
SSH reverse tunneling is NOT needed. The user pastes a code back from
their laptop browser, identical UX to a device-code flow.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from ai_clis.auth_helpers import git_author_env
from ai_clis.base import AICliAdapter, LoginResult, SpawnArgs

logger = logging.getLogger(__name__)


class ClaudeAdapter(AICliAdapter):
    name = "claude"
    oauth_callback_ports = []  # hosted callback at platform.claude.com

    def spawn_args(self, dev, profile_dir: Path) -> SpawnArgs:
        gitconfig = str(profile_dir / ".gitconfig")
        env = {
            "HOME": str(profile_dir),
            "GIT_CONFIG_GLOBAL": gitconfig,
            **git_author_env(dev),
        }
        return SpawnArgs(env=env, argv_prefix=["claude"])

    def login(self, dev, profile_dir: Path) -> LoginResult:
        profile_dir.mkdir(parents=True, exist_ok=True)
        (profile_dir / ".claude").mkdir(exist_ok=True)

        env = {**os.environ, "HOME": str(profile_dir)}
        try:
            result = subprocess.run(
                ["claude", "auth", "login"],
                env=env,
                check=False,
            )
        except FileNotFoundError:
            return LoginResult(
                success=False,
                error="claude CLI not found on PATH",
                hint="Install Claude Code: https://docs.claude.com/en/docs/claude-code/quickstart",
            )

        if result.returncode != 0:
            return LoginResult(
                success=False,
                error=f"claude auth login exited with code {result.returncode}",
                hint="Re-run `devbrain login --dev <id> --cli claude` and complete the OAuth flow in your laptop browser.",
            )

        if not self.is_logged_in(dev, profile_dir):
            return LoginResult(
                success=False,
                error="claude auth login completed but ~/.claude.json was not written under the profile",
                hint=f"Check {profile_dir}/.claude.json exists.",
            )

        return LoginResult(success=True)

    def is_logged_in(self, dev, profile_dir: Path) -> bool:
        return (profile_dir / ".claude.json").exists()

    def required_dotfiles(self) -> list[str]:
        return [".claude.json", ".claude/", ".gitconfig"]


default_register = True
if default_register:
    from ai_clis.base import default_registry

    default_registry.register(ClaudeAdapter)
