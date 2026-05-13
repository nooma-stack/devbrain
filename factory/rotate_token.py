"""Token rotation: mint a fresh Anthropic OAuth token and atomically
replace either the system-wide token in .env or a single dev's profile
file.

Two flavors:

  * **System token** (`rotate_system_token`) — writes to
    `<DEVBRAIN_HOME>/.env`'s `DEVBRAIN_COGNIFY_OAUTH_TOKEN=` line. This
    is the system fallback used by scheduled cognify-extract /
    cognify-edges launchd jobs.

  * **Dev token** (`rotate_dev_token`) — writes to
    `<DEVBRAIN_HOME>/profiles/<dev_id>/.claude/oauth-token`. This is
    the dev's personal token used by their interactive Claude sessions
    AND by cognify when triggered from their `end_session`.

Both flavors:

  1. Back up the existing token file (.env or oauth-token) to
     `<path>.pre-rotate.<timestamp>` for one-step rollback.
  2. Run `claude setup-token` interactively. The operator (you)
     authenticates in a browser. The setup-token output (captured via
     `script(1)`) is scraped for the new token.
  3. Atomically replace the target file with the new value (temp
     file + rename, chmod 0600).
  4. Optionally reload cognify launchd jobs (system flavor only) so the
     new token takes effect immediately rather than at next plist load.

The setup-token capture machinery is shared with `ai_clis.claude` —
see `_run_setup_token_and_extract()` below, which factors the
script(1)+regex pattern out of `ClaudeAdapter.login()`.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RotationResult:
    """Outcome of a token rotation. ``success`` flags whether the new
    token landed on disk; ``backup_path`` is where the old file was
    moved (None on first-ever provisioning). ``token_preview`` is the
    new token's first 25 chars (safe to log)."""

    success: bool
    target_path: Path
    backup_path: Path | None
    token_preview: str | None
    error: str | None = None
    hint: str | None = None


def rotate_system_token(
    devbrain_home: Path | None = None,
    *,
    reload_launchd: bool = True,
    runner: callable = subprocess.run,
    setup_token_fn: callable | None = None,
) -> RotationResult:
    """Rotate the system DEVBRAIN_COGNIFY_OAUTH_TOKEN in `.env`.

    Args:
      devbrain_home: defaults to the repo root inferred from this file.
      reload_launchd: after rotation, run `launchctl unload && load`
        on the cognify-extract + cognify-edges plists so the new token
        takes effect immediately. Set False for tests / when launchd
        isn't relevant.
      runner: dependency-injected subprocess.run for tests.
      setup_token_fn: dependency-injected callable to mint a token; takes
        a runner callable, returns the new token string or raises. Defaults
        to a real `claude setup-token` invocation. Tests pass a stub.
    """
    if devbrain_home is None:
        devbrain_home = Path(__file__).resolve().parent.parent
    env_path = devbrain_home / ".env"

    if setup_token_fn is None:
        setup_token_fn = _run_setup_token_and_extract

    try:
        new_token = setup_token_fn(runner=runner)
    except RotationError as exc:
        return RotationResult(
            success=False,
            target_path=env_path,
            backup_path=None,
            token_preview=None,
            error=exc.error,
            hint=exc.hint,
        )

    backup_path = _backup_file(env_path)
    _replace_env_var(env_path, "DEVBRAIN_COGNIFY_OAUTH_TOKEN", new_token)

    if reload_launchd:
        _reload_cognify_launchd(runner=runner)

    return RotationResult(
        success=True,
        target_path=env_path,
        backup_path=backup_path,
        token_preview=_safe_preview(new_token),
    )


def rotate_dev_token(
    dev_id: str,
    devbrain_home: Path | None = None,
    *,
    runner: callable = subprocess.run,
    setup_token_fn: callable | None = None,
) -> RotationResult:
    """Rotate a single dev's `profiles/<dev_id>/.claude/oauth-token`."""
    if devbrain_home is None:
        devbrain_home = Path(__file__).resolve().parent.parent

    profile_dir = devbrain_home / "profiles" / dev_id
    token_path = profile_dir / ".claude" / "oauth-token"

    if not profile_dir.exists():
        return RotationResult(
            success=False,
            target_path=token_path,
            backup_path=None,
            token_preview=None,
            error=f"profile directory not found: {profile_dir}",
            hint=(
                f"Run `devbrain setup add-dev` to onboard {dev_id} first, "
                "or check the dev_id spelling."
            ),
        )

    if setup_token_fn is None:
        setup_token_fn = _run_setup_token_and_extract

    try:
        new_token = setup_token_fn(runner=runner)
    except RotationError as exc:
        return RotationResult(
            success=False,
            target_path=token_path,
            backup_path=None,
            token_preview=None,
            error=exc.error,
            hint=exc.hint,
        )

    backup_path = _backup_file(token_path) if token_path.exists() else None
    _atomic_write(token_path, new_token, mode=0o600)

    return RotationResult(
        success=True,
        target_path=token_path,
        backup_path=backup_path,
        token_preview=_safe_preview(new_token),
    )


# ─── Internals ───────────────────────────────────────────────────────────────


class RotationError(Exception):
    """Raised when the setup-token flow fails. Carries operator-friendly
    `error` + `hint` strings so the CLI can print actionable guidance."""

    def __init__(self, error: str, hint: str = ""):
        super().__init__(error)
        self.error = error
        self.hint = hint


# Token-capture regex: matches sk-ant-oatN-... where N is one or more
# digits. Mirrors ai_clis/claude.py's _extract_oauth_token; replicated
# here to avoid a circular import when ai_clis depends on rotate_token.
_TOKEN_RE = re.compile(
    r"sk-ant-oat\d+-[A-Za-z0-9_-]+(?:-[A-Za-z0-9_-]+)*"
)
# Intra-token TTY escapes that need stripping before regex match.
_INTRA_TOKEN_ESCAPE_RE = re.compile(r"(?:\x1b\[1C|\r\x1b\[1B)")


def _run_setup_token_and_extract(*, runner: callable) -> str:
    """Run `claude setup-token` via script(1) and scrape the issued token.

    Raises `RotationError` on any failure path with operator guidance.
    """
    log_fd, log_path_str = tempfile.mkstemp(prefix=".setup-token-", suffix=".log")
    os.close(log_fd)
    log_path = Path(log_path_str)
    os.chmod(log_path, 0o600)

    try:
        try:
            result = runner(
                ["script", "-q", str(log_path), "claude", "setup-token"],
                check=False,
            )
        except FileNotFoundError as exc:
            raise RotationError(
                error=f"binary not found: {exc.filename or 'script or claude'}",
                hint=(
                    "Both `script` (system-provided on macOS) and `claude` "
                    "must be on PATH for token rotation to work."
                ),
            ) from exc

        if result.returncode != 0:
            raise RotationError(
                error=f"claude setup-token exited with code {result.returncode}",
                hint=(
                    "Complete the OAuth flow: open the printed URL in your "
                    "browser, sign in, copy the verification code from the "
                    "post-signin page, and paste it back into this session."
                ),
            )

        log_text = log_path.read_bytes().decode("utf-8", "replace")
    finally:
        try:
            log_path.unlink()
        except OSError:
            pass

    cleaned = _INTRA_TOKEN_ESCAPE_RE.sub("", log_text)
    match = _TOKEN_RE.search(cleaned)
    if match is None:
        raise RotationError(
            error="claude setup-token completed but no sk-ant-oatN-... token was found in the captured output",
            hint=(
                "The OAuth flow may not have completed — verify you signed "
                "in and pasted the verification code."
            ),
        )
    return match.group(0)


def _backup_file(path: Path) -> Path | None:
    """Copy `path` to `path.pre-rotate.<unix_ts>`. Returns the backup
    path, or None if the original didn't exist."""
    if not path.exists():
        return None
    import time
    ts = int(time.time())
    backup = path.with_suffix(path.suffix + f".pre-rotate.{ts}")
    shutil.copy2(path, backup)
    os.chmod(backup, 0o600)
    return backup


def _replace_env_var(env_path: Path, key: str, value: str) -> None:
    """Update `key=value` in the .env at `env_path`, preserving every
    other line. If the file doesn't exist, create it. If the key isn't
    present, append it. Atomic via temp file + rename.

    Preserves blank lines + comment lines verbatim.
    """
    if env_path.exists():
        original = env_path.read_text().splitlines(keepends=False)
    else:
        original = []

    new_lines: list[str] = []
    found = False
    for line in original:
        stripped = line.lstrip()
        if stripped.startswith("#") or not stripped:
            new_lines.append(line)
            continue
        # Match `KEY=...` (allow leading `export `)
        bare = stripped[len("export "):] if stripped.startswith("export ") else stripped
        if bare.startswith(f"{key}="):
            new_lines.append(f"{key}={value}")
            found = True
        else:
            new_lines.append(line)

    if not found:
        # Append (after a comment header for discoverability)
        if new_lines and new_lines[-1] != "":
            new_lines.append("")
        new_lines.append(f"# DevBrain cognify system token (see config/devbrain.yaml).")
        new_lines.append(f"{key}={value}")

    # Atomic write
    env_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=env_path.name + ".", suffix=".tmp", dir=str(env_path.parent)
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(new_lines))
            f.write("\n")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, env_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _atomic_write(target: Path, content: str, *, mode: int = 0o600) -> None:
    """Write `content` to `target` atomically, set mode."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _reload_cognify_launchd(*, runner: callable) -> None:
    """Reload the two cognify LLM-cost launchd plists so they pick up
    the new env var values. Errors are logged-and-ignored — rotation
    succeeded DB-side; the operator can manually `launchctl load` if
    this fails.
    """
    home = Path.home()
    plists = [
        home / "Library" / "LaunchAgents" / "com.devbrain.cognify-extract.plist",
        home / "Library" / "LaunchAgents" / "com.devbrain.cognify-edges.plist",
    ]
    for plist in plists:
        if not plist.exists():
            continue
        runner(
            ["launchctl", "unload", str(plist)],
            check=False,
            capture_output=True,
        )
        runner(
            ["launchctl", "load", str(plist)],
            check=False,
            capture_output=True,
        )


def _safe_preview(token: str) -> str:
    """Return a token preview safe to print: prefix + ellipsis + last 4."""
    if len(token) < 30:
        return token[:8] + "…"
    return token[:25] + "…" + token[-4:]
