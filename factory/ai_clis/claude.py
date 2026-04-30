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

Credential storage: Claude Code stores OAuth tokens in macOS Keychain
(service "Claude Code-credentials"). The Security framework resolves
the user's login keychain via $HOME/Library/Keychains/login.keychain-db,
so the HOME-swap correctly redirects which keychain is consulted —
provided that file exists at the swapped path. We provision a per-dev
keychain at <profile>/Library/Keychains/login.keychain-db on first
login. The keychain password is stashed at
<profile>/.claude/.keychain-password (mode 600); it's read by the
factory's cli_executor before each spawn (the keychain locks between
subprocess invocations because there's no GUI Security agent in factory
contexts).
"""

from __future__ import annotations

import logging
import os
import secrets
import subprocess
from pathlib import Path

from ai_clis.auth_helpers import git_author_env
from ai_clis.base import AICliAdapter, LoginResult, SpawnArgs

logger = logging.getLogger(__name__)

# Paths inside the profile directory.
_KEYCHAIN_REL = Path("Library") / "Keychains" / "login.keychain-db"
_PASSWORD_REL = Path(".claude") / ".keychain-password"


def _read_or_generate_keychain_password(profile_dir: Path) -> str:
    """Return the per-profile keychain password, generating + persisting if absent.

    If the user (via cli.py login) pre-wrote a custom password to the file,
    this returns that password. Otherwise generates a random
    URL-safe token and persists it (mode 600).
    """
    pw_file = profile_dir / _PASSWORD_REL
    if pw_file.exists():
        return pw_file.read_text().strip()
    pw = secrets.token_urlsafe(24)
    pw_file.parent.mkdir(parents=True, exist_ok=True)
    pw_file.write_text(pw)
    pw_file.chmod(0o600)
    return pw


def _ensure_keychain(profile_dir: Path) -> Path:
    """Create + unlock the per-profile keychain. Idempotent.

    On macOS, claude's auth flow looks up creds via Security framework's
    `kSecPreferencesDomainUser` lookup, which resolves the keychain file
    via $HOME/Library/Keychains/login.keychain-db. With HOME=<profile>,
    that path lives under the profile dir — and we have to create the
    keychain there ourselves (macOS doesn't auto-create it for synthetic
    homes). On non-macOS this is a no-op (Security framework absent;
    claude falls back to whatever its file-based path is).

    Returns the keychain path. Raises subprocess.CalledProcessError if
    the keychain can't be created (genuinely fatal — caller should bubble
    up).
    """
    keychain = profile_dir / _KEYCHAIN_REL
    keychain.parent.mkdir(parents=True, exist_ok=True)
    password = _read_or_generate_keychain_password(profile_dir)

    if not keychain.exists():
        subprocess.run(
            ["security", "create-keychain", "-p", password, str(keychain)],
            check=True, capture_output=True,
        )
        # Disable auto-lock — factory orchestrator spawns can't pop a
        # GUI dialog to re-enter the password. The keychain still locks
        # between subprocess invocations (per-process security agent
        # state), so cli_executor.run_cli runs `security unlock-keychain`
        # before each spawn.
        subprocess.run(
            ["security", "set-keychain-settings", str(keychain)],
            check=False, capture_output=True,
        )
        logger.info("Provisioned per-dev keychain at %s", keychain)

    # Unlock for the upcoming claude auth login subprocess (otherwise
    # macOS pops a password prompt the user shouldn't see).
    subprocess.run(
        ["security", "unlock-keychain", "-p", password, str(keychain)],
        check=False, capture_output=True,
    )
    return keychain


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

        # Provision the per-profile keychain BEFORE running claude auth
        # login. Without this, claude's credential write goes to the
        # macOS user's login keychain (acct=lhtdev) instead of the
        # per-profile one — defeating multi-dev isolation.
        try:
            _ensure_keychain(profile_dir)
        except subprocess.CalledProcessError as e:
            return LoginResult(
                success=False,
                error=f"failed to provision profile keychain: {e.stderr.decode('utf-8', 'replace') if e.stderr else e}",
                hint=f"Run `devbrain reset-keychain --dev {dev.dev_id}` and re-try.",
            )

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
