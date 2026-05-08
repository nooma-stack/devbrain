"""Claude Code CLI adapter.

Claude Code does NOT expose a config-dir env var (per official docs at
code.claude.com/docs/en/settings — the only customizable path is
`autoMemoryDirectory` in settings.json). To isolate per-dev creds we
have to swap HOME for the spawned subprocess.

The HOME swap is constrained to the single AI subprocess invocation —
the orchestrator's HOME and broader environment stay untouched. Git
authorship is set explicitly via GIT_CONFIG_GLOBAL + GIT_AUTHOR_* env
vars on top of the HOME swap (these win over .gitconfig discovery).

Login flow: `claude setup-token` runs server-side inside the dev's
profile dir, captured via script(1) so the dev's interactive SSH
session can paste the OAuth verification code while we still scrape
stdout for the issued token. The token (sk-ant-oat01-...) is stashed
at <profile>/.claude/oauth-token (mode 600); cli_executor reads it
into CLAUDE_CODE_OAUTH_TOKEN env before each Claude spawn (env var
takes precedence over keychain per Anthropic's auth precedence rules).

Auto-browser-launch suppression: claude setup-token tries to open a
browser before falling back to print-URL. On the Mac Studio (server
context) any actual browser open would target lhtdev's GUI session,
not the dev's local browser. We plant a fail-fast `open` and
`xdg-open` shim at <profile>/.claude/.devbrain-fakebin and prepend
it to PATH so the auto-open returns nonzero and claude immediately
falls back to printing the URL.

Earlier (2026-04) attempts used `claude auth login` and concluded
the browser-on-different-machine flow was impossible. `setup-token`
is a separate command designed for headless/CI contexts and supports
the print-URL + paste-code-back path; see
docs/plans/2026-05-07-onboarding-server-side-auth-design.md.

Keychain provisioning is preserved for compatibility with cli.py's
reset-keychain command and cli_executor's optional keychain-unlock
fallback, but the runtime path uses the env var, not the keychain.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import subprocess
import tempfile
from pathlib import Path

from ai_clis.auth_helpers import git_author_env
from ai_clis.base import AICliAdapter, LoginResult, SpawnArgs

logger = logging.getLogger(__name__)

# Paths inside the profile directory.
_KEYCHAIN_REL = Path("Library") / "Keychains" / "login.keychain-db"
_PASSWORD_REL = Path(".claude") / ".keychain-password"
_OAUTH_TOKEN_REL = Path(".claude") / "oauth-token"
_FAKEBIN_REL = Path(".claude") / ".devbrain-fakebin"

# `claude setup-token` prints the issued token in its TTY output. Two
# wrinkles caught in the 2026-05-08 E2E test:
#   1. Anthropic's actual prefix is `sk-ant-oatN-` where N is one or
#      more digits (observed `sk-ant-oat1-` against claude 2.1.132,
#      vs `sk-ant-oat01-` shown in their docs). Match a digit run.
#   2. `script(1)` captures the rendered TTY stream including cursor-
#      positioning escapes that claude injects MID-TOKEN to display
#      it across visual lines (`\x1b[1C` cursor right, `\r\x1b[1B`
#      CR + cursor down 1 = visual line wrap). We strip those so the
#      token reassembles contiguously, but we LEAVE post-token escapes
#      like `\x1b[2B` (cursor down 2 = paragraph break) intact so the
#      regex stops at the right place rather than slurping the next
#      message ("Store this token securely…").
_OAUTH_TOKEN_RE = re.compile(r"sk-ant-oat\d+-[A-Za-z0-9_\-]+")
_TOKEN_NOISE_RE = re.compile(r"\x1b\[1C|\r\x1b\[1B")


def _read_or_generate_keychain_password(profile_dir: Path) -> str:
    """Return the per-profile keychain password, generating + persisting if absent.

    If the user (via cli.py login) pre-wrote a custom password to the file,
    this returns that password. Otherwise generates a random URL-safe token
    and persists it (mode 600).
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

    # No-op gracefully on non-macOS (e.g., Linux CI runners). The
    # `security` binary is shipped only with macOS; if it's absent,
    # claude's auth flow falls through to whatever its non-Keychain
    # path is. This keeps the test suite green on Linux runners and
    # makes the code portable for any future Linux factory host.
    try:
        if not keychain.exists():
            subprocess.run(
                ["security", "create-keychain", "-p", password, str(keychain)],
                check=True, capture_output=True,
            )
            # Disable auto-lock — factory orchestrator spawns can't pop a
            # GUI dialog to re-enter the password. The keychain still
            # locks between subprocess invocations (per-process security
            # agent state), so cli_executor.run_cli runs `security
            # unlock-keychain` before each spawn.
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
    except FileNotFoundError:
        logger.debug("`security` binary not found — non-macOS host, skipping keychain provisioning")
    return keychain


def _extract_oauth_token(text: str) -> str | None:
    """Find the first sk-ant-oatN-... token in claude setup-token output.

    Strips intra-token cursor-positioning escapes (cursor-right and
    single-line wraps) so the token reassembles contiguously. Post-
    token escapes (cursor-down-2 and beyond) are deliberately left in
    place so the regex stops at the message boundary rather than
    slurping subsequent text.
    """
    cleaned = _TOKEN_NOISE_RE.sub("", text)
    match = _OAUTH_TOKEN_RE.search(cleaned)
    return match.group(0) if match else None


def _plant_fakebin(profile_dir: Path) -> Path:
    """Create fail-fast `open` and `xdg-open` shims under the profile dir.

    Returns the bin dir so callers can prepend it to PATH for the spawned
    claude subprocess. Idempotent.
    """
    fakebin = profile_dir / _FAKEBIN_REL
    fakebin.mkdir(parents=True, exist_ok=True)
    for cmd in ("open", "xdg-open"):
        p = fakebin / cmd
        p.write_text("#!/bin/sh\nexit 1\n")
        p.chmod(0o755)
    return fakebin


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

        try:
            _ensure_keychain(profile_dir)
        except subprocess.CalledProcessError as e:
            return LoginResult(
                success=False,
                error=f"failed to provision profile keychain: {e.stderr.decode('utf-8', 'replace') if e.stderr else e}",
                hint=f"Run `devbrain reset-keychain --dev {dev.dev_id}` and re-try.",
            )

        fakebin = _plant_fakebin(profile_dir)
        env = {
            **os.environ,
            "HOME": str(profile_dir),
            "PATH": f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}",
            "BROWSER": "/bin/false",
            "DISPLAY": "",
        }

        # script(1) gives claude a real pty (so the paste-code-back prompt
        # works for the dev's interactive SSH session) while logging all
        # output to a file we scrape for the issued token after exit. macOS
        # BSD script syntax: `script -q <log> <command...>`.
        log_dir = profile_dir / ".claude"
        log_fd, log_path_str = tempfile.mkstemp(
            dir=str(log_dir), prefix=".setup-token-", suffix=".log"
        )
        os.close(log_fd)
        log_path = Path(log_path_str)
        os.chmod(log_path, 0o600)

        try:
            try:
                result = subprocess.run(
                    ["script", "-q", str(log_path), "claude", "setup-token"],
                    env=env,
                    check=False,
                )
            except FileNotFoundError as e:
                return LoginResult(
                    success=False,
                    error=f"binary not found: {e.filename or 'script or claude'}",
                    hint="Both `script` (system-provided on macOS) and `claude` must be on PATH on the factory host.",
                )

            if result.returncode != 0:
                return LoginResult(
                    success=False,
                    error=f"claude setup-token exited with code {result.returncode}",
                    hint=(
                        "Re-run `devbrain login --dev <id> --cli claude` and complete "
                        "the OAuth flow: open the printed URL in your local browser, "
                        "sign in, copy the verification code from the post-signin page, "
                        "and paste it back into this SSH session."
                    ),
                )

            log_text = log_path.read_bytes().decode("utf-8", "replace")
        finally:
            try:
                log_path.unlink()
            except OSError:
                pass

        token = _extract_oauth_token(log_text)
        if token is None:
            return LoginResult(
                success=False,
                error="claude setup-token completed but no sk-ant-oatN-... token found in captured output",
                hint="The OAuth flow may not have completed — verify you signed in and pasted the verification code.",
            )

        token_path = profile_dir / _OAUTH_TOKEN_REL
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(token)
        token_path.chmod(0o600)

        return LoginResult(success=True)

    def is_logged_in(self, dev, profile_dir: Path) -> bool:
        return (profile_dir / _OAUTH_TOKEN_REL).exists()

    def required_dotfiles(self) -> list[str]:
        return [str(_OAUTH_TOKEN_REL), ".claude/", ".gitconfig"]


default_register = True
if default_register:
    from ai_clis.base import default_registry

    default_registry.register(ClaudeAdapter)
