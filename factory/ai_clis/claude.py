"""Claude Code CLI adapter.

Claude Code does NOT expose a config-dir env var (per official docs at
code.claude.com/docs/en/settings — the only customizable path is
`autoMemoryDirectory` in settings.json). To isolate per-dev creds we
have to swap HOME for the spawned subprocess.

The HOME swap is constrained to the single AI subprocess invocation —
the orchestrator's HOME and broader environment stay untouched. Git
authorship is set explicitly via GIT_CONFIG_GLOBAL + GIT_AUTHOR_* env
vars on top of the HOME swap (these win over .gitconfig discovery).

Login flow: Claude's auth uses PKCE OAuth with a localhost HTTP
listener that claude.com's hosted callback page hits via JavaScript to
deliver the OAuth code. When the dev's browser is on the same machine
as the CLI (RDP / local terminal), the localhost callback completes
auto-magically. Over SSH (browser on a different machine than the CLI),
the dev's laptop browser can't reach the Mac Studio's localhost — so
DevBrain instead drives a HEADLESS Chromium on the Mac Studio that
visits the OAuth callback URL with the code+state the dev pastes back.
The headless browser executes the same JS as the dev's normal browser
would, hits the localhost listener, claude completes the token
exchange, writes credentials to the per-profile keychain. Empirically
verified during the 2026-04-30 SSH onboarding bring-up — see
`docs/plans/2026-04-29-overnight-handoff.md` "Phase 6 Outcome" section.

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
import tempfile
import time
import urllib.parse
from pathlib import Path

import click

from ai_clis.auth_helpers import git_author_env
from ai_clis.base import AICliAdapter, LoginResult, SpawnArgs

logger = logging.getLogger(__name__)

# Regex matching Claude's OAuth authorization URL — used to extract the URL
# from claude's terminal output. The capture group grabs everything up to
# the next whitespace so we get the full URL with all query params.
_OAUTH_URL_RE = re.compile(rb"https://claude\.com/cai/oauth/authorize\S+")
# String emitted by claude on successful auth (post-token-exchange). Used
# as the terminal-state sentinel so we can stop reading the PTY and reap.
_LOGIN_OK_SENTINEL = b"Login successful"
# Hard timeout for the entire auth flow including the human OAuth dance.
_AUTH_TIMEOUT_SECONDS = 600
# How long to wait for headless browser to fire the localhost callback
# and for claude to write tokens to the keychain.
_CALLBACK_TIMEOUT_SECONDS = 60

# Chromium-based browsers we'll search for, in preference order. Any of
# these can run in headless mode with Chrome's CLI flags, which is all
# we need to drive the OAuth callback page's JavaScript.
_CHROMIUM_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Arc.app/Contents/MacOS/Arc",
]


def find_chromium_browser() -> Path | None:
    """Return path to a chromium-based browser usable in headless mode.

    Public so devdoctor + onboarding tooling can surface a clear
    install hint when no compatible browser is present.
    """
    for candidate in _CHROMIUM_CANDIDATES:
        p = Path(candidate)
        if p.exists() and os.access(p, os.X_OK):
            return p
    return None

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


def _drive_headless_callback(
    code: str,
    state: str,
    profile_dir: Path,
) -> bool:
    """Drive a headless Chromium that visits Claude's OAuth callback page.

    The hosted callback page at platform.claude.com/oauth/code/callback
    runs JavaScript that POSTs to localhost:RANDOM_PORT (Claude's listener)
    to deliver the OAuth code. When the dev's browser is on a different
    machine than the CLI, that JS can't reach the listener — but a
    headless Chromium running on the SAME machine as Claude's CLI can.

    Returns True if Claude wrote a "Claude Code-credentials" entry to
    the per-profile keychain within the timeout. Returns False on any
    failure (browser missing, callback didn't fire, claude didn't
    write tokens).
    """
    chrome = find_chromium_browser()
    if chrome is None:
        logger.error(
            "No Chromium-based browser found for headless OAuth callback; "
            "tried: %s",
            ", ".join(_CHROMIUM_CANDIDATES),
        )
        return False

    callback_url = (
        "https://platform.claude.com/oauth/code/callback"
        f"?code={urllib.parse.quote(code, safe='')}"
        f"&state={urllib.parse.quote(state, safe='')}"
    )
    keychain_path = profile_dir / _KEYCHAIN_REL

    with tempfile.TemporaryDirectory(prefix="devbrain-headless-") as tmp_data:
        # `--headless=new` is Chromium's modern headless mode (Chrome 109+).
        # `--virtual-time-budget=10000` lets JS run for 10s of virtual
        # time before exit, so the page's localhost POST has time to
        # complete. `--user-data-dir` keeps cookies/storage isolated to
        # this transient session — nothing pollutes the dev's real Chrome
        # profile or the lhtdev system Chrome state.
        argv = [
            str(chrome), "--headless=new",
            "--disable-gpu", "--no-sandbox",
            f"--user-data-dir={tmp_data}",
            "--virtual-time-budget=15000",
            "--disable-features=Translate",
            callback_url,
        ]
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as e:
            logger.error("Failed to spawn headless browser: %s", e)
            return False

        # Poll for claude to finish the token exchange and write to the
        # per-profile keychain. We can't watch claude directly because
        # it's a sibling process and our parent doesn't own its waitpid;
        # the keychain entry is the durable proof of completion.
        deadline = time.monotonic() + _CALLBACK_TIMEOUT_SECONDS
        success = False
        while time.monotonic() < deadline:
            check = subprocess.run(
                ["security", "find-generic-password",
                 "-s", "Claude Code-credentials",
                 str(keychain_path)],
                capture_output=True,
            )
            if check.returncode == 0:
                success = True
                break
            time.sleep(1)

        # Always tear down the headless browser.
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception:
            pass

    return success


def _drive_auth_login(env: dict[str, str], profile_dir: Path) -> tuple[bool, bytes]:
    """Run `claude auth login` and drive its OAuth flow over SSH-friendly rails.

    Approach:
      1. pty.fork the child running `claude auth login --claudeai`. PTY
         (vs plain Popen) so claude renders interactively and we get its
         stdout in real-time, not just on close.
      2. Read the master fd until the OAuth URL appears.
      3. Print the URL to the user via DevBrain's CLI prompt; user OAuths
         in their laptop browser.
      4. Collect the code+state from the user via click.prompt.
      5. Spawn a headless Chromium on this machine that visits Claude's
         hosted callback URL with the user's code+state. The hosted page's
         JavaScript fires the localhost POST to Claude's listener — which
         IS reachable from this same-machine browser, regardless of where
         the dev's actual browser was.
      6. Poll the per-profile keychain until Claude writes the token entry
         (proof of successful exchange).
      7. Reap the claude subprocess.

    Returns (success, full_pty_buffer). success=False covers: timeout,
    user cancellation, missing chromium, and any failure to detect a
    fresh keychain entry within the callback window.
    """
    argv = ["claude", "auth", "login", "--claudeai"]

    pid, master_fd = pty.fork()
    if pid == 0:
        try:
            os.execvpe(argv[0], argv, env)
        except FileNotFoundError:
            os._exit(127)
        except Exception:
            os._exit(126)
        return  # unreachable

    buffer = b""
    deadline = time.monotonic() + _AUTH_TIMEOUT_SECONDS
    callback_succeeded = False

    def _kill_child(sig: int = signal.SIGTERM) -> None:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass

    try:
        # ─── Phase 1: read PTY until OAuth URL surfaces ───────────
        url: str | None = None
        while url is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("Timed out waiting for claude OAuth URL")
                _kill_child()
                break
            try:
                ready, _, _ = select.select([master_fd], [], [], min(remaining, 5.0))
            except (OSError, InterruptedError):
                continue
            if not ready:
                continue
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                break  # slave closed
            if not chunk:
                break
            buffer += chunk
            logger.debug("claude PTY chunk: %r", chunk[:200])
            m = _OAUTH_URL_RE.search(buffer)
            if m:
                url = m.group(0).decode("utf-8", "replace")

        if url is None:
            return False, buffer

        # Extract the `state` we'll need to round-trip back to claude.
        state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("state", [""])[0]

        # ─── Phase 2: ask user to OAuth + paste code back ─────────
        click.echo()
        click.echo("Open this URL in your laptop browser to authorize Claude Code:")
        click.echo()
        click.echo(f"  {url}")
        click.echo()
        click.echo("After signing in, claude.com will display an auth code on the page.")
        click.echo("Copy the FULL code (including everything after '#') and paste it here.")
        raw = click.prompt("Auth code", default="", show_default=False).strip()
        if not raw:
            click.echo("(empty input — cancelling auth)", err=True)
            _kill_child(signal.SIGINT)
            return False, buffer

        # Handle both `<code>#<state>` and bare `<code>` formats.
        code_part = raw.split("#", 1)[0]

        # ─── Phase 3: drive headless browser to fire localhost callback ───
        click.echo("Completing auth via headless browser…")
        callback_succeeded = _drive_headless_callback(code_part, state, profile_dir)

        # ─── Phase 4: drain remaining PTY output, reap child ──────
        # claude should exit on its own once the token exchange completes.
        # Drain to keep the kernel's PTY buffer from blocking it on write,
        # and to capture any final stderr for logging.
        try:
            os.set_blocking(master_fd, False)
        except OSError:
            pass
        drain_deadline = time.monotonic() + 5
        while time.monotonic() < drain_deadline:
            try:
                chunk = os.read(master_fd, 4096)
                if not chunk:
                    break
                buffer += chunk
            except (BlockingIOError, OSError):
                time.sleep(0.2)

        # If claude is still running after the headless callback succeeded,
        # send SIGTERM — it may have a polling loop waiting indefinitely.
        if callback_succeeded:
            _kill_child()

        try:
            _, _status = os.waitpid(pid, 0)
        except ChildProcessError:
            pass

    except KeyboardInterrupt:
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

    return callback_succeeded, buffer


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

        # Pre-flight: claude has to be on PATH. Surface a specific hint
        # instead of letting pty.fork+execvpe ENOENT bubble up after we've
        # already provisioned the keychain.
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

        # Pre-flight: check Chromium is installed before starting OAuth
        # so we don't get the dev halfway through and then bail.
        if find_chromium_browser() is None:
            return LoginResult(
                success=False,
                error="No Chromium-based browser found for OAuth callback",
                hint="Install Chrome: brew install --cask google-chrome  (or run: devbrain devdoctor)",
            )

        # Drive the auth flow: PTY-capture claude's URL, get code from
        # user, drive headless browser to fire localhost callback. See
        # _drive_auth_login docstring for the full architecture.
        try:
            success, output = _drive_auth_login(env, profile_dir)
        except KeyboardInterrupt:
            return LoginResult(
                success=False,
                error="claude auth login cancelled by user (Ctrl+C)",
                hint="Re-run `devbrain login --dev <id> --cli claude` when ready.",
            )

        if not success:
            tail = output[-500:].decode("utf-8", "replace") if output else ""
            logger.debug("claude auth output (tail): %r", tail)
            return LoginResult(
                success=False,
                error="OAuth callback didn't complete — keychain entry not written within timeout",
                hint=(
                    "Re-run `devbrain login --dev <id> --cli claude`. If repeated, "
                    "check that headless Chrome can reach localhost (no firewall blocking) "
                    "and that the keychain isn't locked: `devbrain reset-keychain --dev <id>`."
                ),
            )

        if not self.is_logged_in(dev, profile_dir):
            return LoginResult(
                success=False,
                error="claude reported success but ~/.claude.json was not written under the profile",
                hint=f"Check {profile_dir}/.claude.json exists; consider reset-keychain.",
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
