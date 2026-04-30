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
import pty
import re
import secrets
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import click

from ai_clis.auth_helpers import git_author_env
from ai_clis.base import AICliAdapter, LoginResult, SpawnArgs

logger = logging.getLogger(__name__)

# Regex matching Claude's OAuth authorization URL — used by the PTY-driven
# auth flow to extract the URL from claude's terminal output without
# depending on the surrounding prose (which has changed across versions).
_OAUTH_URL_RE = re.compile(rb"https://claude\.com/cai/oauth/authorize\S+")
# String emitted by claude on successful auth (post-token-exchange). Used
# as the terminal-state sentinel so we can stop reading the PTY and reap.
_LOGIN_OK_SENTINEL = b"Login successful"
# Hard timeout for the entire auth flow including human OAuth dance.
_AUTH_TIMEOUT_SECONDS = 600

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


def _pty_auth_login(env: dict[str, str]) -> tuple[int, bytes]:
    """Run `claude auth login` under a PTY, driving I/O ourselves.

    Why PTY: claude's auth flow uses a raw-mode TTY (Ink-based React TUI)
    for the auth-code paste step. That input path bypasses normal
    pipe-based stdin — neither subprocess.run() with input=, nor
    `tmux send-keys`, nor `tmux paste-buffer` can deliver keystrokes
    that claude reads. A pty.fork() gives us a real terminal pair where
    claude's `read(/dev/tty)` consumes the bytes we write to the master fd.

    Why DevBrain owns the prompt: the alternative — letting claude print
    its own URL and Ink-render its own prompt — only works on a fully
    interactive macOS GUI session. Over SSH or when DevBrain is the
    parent shell, the Ink prompt is unreachable and the dev sees a hung
    cursor. Instead, we suppress claude's stdout, parse for the OAuth
    URL ourselves, and use click.prompt() to collect the code via a
    well-behaved cooked-mode read.

    Returns (exit_code, full_output_buffer). exit_code = -1 if the
    process was killed for timeout/cancellation.
    """
    # Build the claude argv. We pass --claudeai explicitly so behaviour
    # is deterministic regardless of which default Anthropic ships next.
    argv = ["claude", "auth", "login", "--claudeai"]

    pid, master_fd = pty.fork()
    if pid == 0:
        # ─── child ────────────────────────────────────────────────
        # Replace ourselves with claude. On exec failure we exit
        # nonzero so the parent's waitpid() sees a real status.
        try:
            os.execvpe(argv[0], argv, env)
        except FileNotFoundError:
            os._exit(127)
        except Exception:
            os._exit(126)
        return  # unreachable

    # ─── parent ──────────────────────────────────────────────────
    # We do NOT print claude's stdout to the user. Claude's noisy
    # "Opening browser to sign in… If the browser didn't open, visit:..."
    # prose is replaced by our own clean prompt below. Everything we
    # capture stays in `buffer` for parsing + post-mortem logging.
    buffer = b""
    url_handled = False
    deadline = time.monotonic() + _AUTH_TIMEOUT_SECONDS
    exit_code: int | None = None

    def _kill_child(sig: int = signal.SIGTERM) -> None:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("claude auth login timed out after %ds", _AUTH_TIMEOUT_SECONDS)
                _kill_child()
                break

            try:
                ready, _, _ = select.select([master_fd], [], [], min(remaining, 5.0))
            except (OSError, InterruptedError):
                continue

            if ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    # Slave end closed — child is exiting.
                    break
                if not chunk:
                    break
                buffer += chunk
                logger.debug("claude PTY chunk: %r", chunk[:200])

                # Successful flow may complete before we ever ask for a
                # code (some auth paths finish via the hosted callback's
                # back-channel). If we see the sentinel, bail out clean.
                if _LOGIN_OK_SENTINEL in buffer:
                    break

            # Detect URL once and hand control to the user. Don't gate
            # on `ready` — the URL might already be in the buffer from
            # an earlier chunk.
            if not url_handled:
                m = _OAUTH_URL_RE.search(buffer)
                if m:
                    url_handled = True
                    url = m.group(0).decode("utf-8", "replace")
                    click.echo()
                    click.echo("Open this URL in your laptop browser to authorize Claude Code:")
                    click.echo()
                    click.echo(f"  {url}")
                    click.echo()
                    click.echo(
                        "After 'You're all set up for Claude Code' shows in the browser, "
                        "claude.com may display an auth code on the page. If so, copy it.",
                    )
                    code = click.prompt(
                        "Paste auth code (or just press Enter if no code was shown)",
                        default="",
                        show_default=False,
                    ).strip()
                    # Send whatever the user gave us, terminated by a newline.
                    # Claude's auth subprocess reads from /dev/tty (which is
                    # our master_fd here); the empty-string + newline
                    # case is harmless if claude already auto-completed
                    # (the write may go to a closed slave; we swallow that).
                    payload = (code + "\n").encode("utf-8")
                    try:
                        os.write(master_fd, payload)
                    except OSError:
                        # Slave closed — claude exited via polling already.
                        pass
                    click.echo("Verifying with Anthropic…")

        # Child should exit shortly after we either saw the sentinel or
        # killed it. Reap the status without hanging forever.
        try:
            _, status = os.waitpid(pid, 0)
            if os.WIFEXITED(status):
                exit_code = os.WEXITSTATUS(status)
            elif os.WIFSIGNALED(status):
                exit_code = -1
            else:
                exit_code = -1
        except ChildProcessError:
            exit_code = -1
    except KeyboardInterrupt:
        # Ctrl+C: kill claude and propagate.
        _kill_child(signal.SIGINT)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        raise
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass

    return (exit_code if exit_code is not None else -1), buffer


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

        # Pre-flight: claude has to be on PATH. If not, fail fast with a
        # specific hint instead of letting pty.fork+execvpe surface a
        # generic ENOENT after we've already lit up the keychain.
        try:
            subprocess.run(
                ["claude", "--version"],
                env=env, check=True, capture_output=True, timeout=10,
            )
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return LoginResult(
                success=False,
                error="claude CLI not found or not responding on PATH",
                hint="Install Claude Code: https://docs.claude.com/en/docs/claude-code/quickstart",
            )

        # Drive claude's auth flow under a PTY so DevBrain owns the I/O
        # end-to-end. See _pty_auth_login docstring for why direct
        # subprocess.run() doesn't work for this command on current
        # claude versions.
        try:
            exit_code, output = _pty_auth_login(env)
        except KeyboardInterrupt:
            return LoginResult(
                success=False,
                error="claude auth login cancelled by user (Ctrl+C)",
                hint="Re-run `devbrain login --dev <id> --cli claude` when ready.",
            )

        if exit_code != 0:
            tail = output[-500:].decode("utf-8", "replace") if output else ""
            logger.debug("claude PTY auth output (tail): %r", tail)
            return LoginResult(
                success=False,
                error=f"claude auth login exited with code {exit_code}",
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
