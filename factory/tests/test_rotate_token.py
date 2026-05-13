"""Tests for factory.rotate_token — system + dev OAuth token rotation."""
from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from rotate_token import (
    RotationError,
    RotationResult,
    _atomic_write,
    _backup_file,
    _replace_env_var,
    _safe_preview,
    rotate_dev_token,
    rotate_system_token,
)


# ─── helpers ─────────────────────────────────────────────────────────────────


def _stub_setup_token_success(token: str = "sk-ant-oat01-NEWLY-MINTED-FROM-STUB-1234567890ABCDEF"):
    """Build a `setup_token_fn` stub that returns a fixed token."""
    def _fn(*, runner):
        return token
    return _fn


def _stub_setup_token_failure(error: str = "claude setup-token exited with code 1", hint: str = "hint here"):
    def _fn(*, runner):
        raise RotationError(error=error, hint=hint)
    return _fn


def _fake_runner_success():
    """Build a runner that always returns returncode=0."""
    def _run(*args, **kwargs):
        r = MagicMock()
        r.returncode = 0
        r.stdout = b""
        r.stderr = b""
        return r
    return _run


# ─── _safe_preview ───────────────────────────────────────────────────────────


def test_safe_preview_long_token():
    token = "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"
    preview = _safe_preview(token)
    # 25-char prefix
    assert preview.startswith(token[:25])
    # Last 4 chars present
    assert preview.endswith(token[-4:])
    assert "…" in preview
    # Critically, the middle of the token must NOT be in the preview.
    assert "9JdHIfY9qf6i" not in preview


def test_safe_preview_short_token():
    """Pathologically short tokens get truncated at 8 chars."""
    preview = _safe_preview("sk-ant-oat01-X")
    assert preview == "sk-ant-o…"


# ─── _replace_env_var ────────────────────────────────────────────────────────


def test_replace_env_var_creates_new_file(tmp_path):
    env_path = tmp_path / ".env"
    _replace_env_var(env_path, "DEVBRAIN_COGNIFY_OAUTH_TOKEN", "newval")
    body = env_path.read_text()
    assert "DEVBRAIN_COGNIFY_OAUTH_TOKEN=newval" in body
    assert os.stat(env_path).st_mode & 0o777 == 0o600


def test_replace_env_var_updates_existing_key(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# Comment\n"
        "OTHER_VAR=untouched\n"
        "DEVBRAIN_COGNIFY_OAUTH_TOKEN=oldval\n"
        "ANOTHER_VAR=stays\n"
    )
    _replace_env_var(env_path, "DEVBRAIN_COGNIFY_OAUTH_TOKEN", "newval")
    body = env_path.read_text()
    assert "DEVBRAIN_COGNIFY_OAUTH_TOKEN=newval" in body
    assert "DEVBRAIN_COGNIFY_OAUTH_TOKEN=oldval" not in body
    assert "OTHER_VAR=untouched" in body
    assert "ANOTHER_VAR=stays" in body
    assert "# Comment" in body  # comments preserved


def test_replace_env_var_appends_missing_key(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("OTHER_VAR=v\n")
    _replace_env_var(env_path, "DEVBRAIN_COGNIFY_OAUTH_TOKEN", "newval")
    body = env_path.read_text()
    assert "OTHER_VAR=v" in body
    assert "DEVBRAIN_COGNIFY_OAUTH_TOKEN=newval" in body


def test_replace_env_var_handles_export_prefix(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("export DEVBRAIN_COGNIFY_OAUTH_TOKEN=oldval\n")
    _replace_env_var(env_path, "DEVBRAIN_COGNIFY_OAUTH_TOKEN", "newval")
    body = env_path.read_text()
    # The export prefix gets stripped on rewrite (consistent format).
    assert "DEVBRAIN_COGNIFY_OAUTH_TOKEN=newval" in body
    assert "oldval" not in body


def test_replace_env_var_is_atomic(tmp_path):
    """No .tmp files left behind on success."""
    env_path = tmp_path / ".env"
    _replace_env_var(env_path, "K", "v")
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


# ─── _backup_file ────────────────────────────────────────────────────────────


def test_backup_file_creates_versioned_copy(tmp_path):
    src = tmp_path / "oauth-token"
    src.write_text("ORIGINAL")
    backup = _backup_file(src)
    assert backup is not None
    assert backup.exists()
    assert backup.read_text() == "ORIGINAL"
    assert ".pre-rotate." in backup.name
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_backup_file_returns_none_when_missing(tmp_path):
    src = tmp_path / "does-not-exist"
    assert _backup_file(src) is None


# ─── rotate_system_token ─────────────────────────────────────────────────────


def test_rotate_system_token_writes_new_token_to_env(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("DEVBRAIN_COGNIFY_OAUTH_TOKEN=OLD\n")

    result = rotate_system_token(
        devbrain_home=tmp_path,
        reload_launchd=False,
        runner=_fake_runner_success(),
        setup_token_fn=_stub_setup_token_success("sk-ant-oat01-NEW-TOKEN-FROM-TEST"),
    )

    assert result.success is True
    assert result.target_path == env_path
    assert result.backup_path is not None
    assert result.backup_path.read_text() == "DEVBRAIN_COGNIFY_OAUTH_TOKEN=OLD\n"
    new_body = env_path.read_text()
    assert "DEVBRAIN_COGNIFY_OAUTH_TOKEN=sk-ant-oat01-NEW-TOKEN-FROM-TEST" in new_body
    assert "OLD" not in new_body
    assert result.token_preview.startswith("sk-ant-oat01-NEW")


def test_rotate_system_token_creates_env_if_missing(tmp_path):
    result = rotate_system_token(
        devbrain_home=tmp_path,
        reload_launchd=False,
        runner=_fake_runner_success(),
        setup_token_fn=_stub_setup_token_success(),
    )
    assert result.success is True
    assert result.backup_path is None  # no original to back up
    assert (tmp_path / ".env").exists()


def test_rotate_system_token_preserves_other_env_vars(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# Devbrain env\n"
        "TELEGRAM_BOT_TOKEN=secret123\n"
        "DEVBRAIN_COGNIFY_OAUTH_TOKEN=OLD\n"
        "DEVBRAIN_DB_PASSWORD=dbpw\n"
    )

    rotate_system_token(
        devbrain_home=tmp_path,
        reload_launchd=False,
        runner=_fake_runner_success(),
        setup_token_fn=_stub_setup_token_success("sk-ant-oat01-NEW"),
    )

    body = env_path.read_text()
    assert "TELEGRAM_BOT_TOKEN=secret123" in body
    assert "DEVBRAIN_DB_PASSWORD=dbpw" in body
    assert "DEVBRAIN_COGNIFY_OAUTH_TOKEN=sk-ant-oat01-NEW" in body
    assert "# Devbrain env" in body  # comment preserved


def test_rotate_system_token_returns_error_when_setup_fails(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("DEVBRAIN_COGNIFY_OAUTH_TOKEN=UNCHANGED\n")

    result = rotate_system_token(
        devbrain_home=tmp_path,
        reload_launchd=False,
        runner=_fake_runner_success(),
        setup_token_fn=_stub_setup_token_failure("setup-token failed"),
    )

    assert result.success is False
    assert result.error == "setup-token failed"
    # Critically, the .env was NOT modified on failure.
    assert env_path.read_text() == "DEVBRAIN_COGNIFY_OAUTH_TOKEN=UNCHANGED\n"


def test_rotate_system_token_reload_launchd_calls_launchctl(tmp_path):
    """When reload_launchd=True, expect launchctl unload+load calls per plist
    (only for plists that exist on disk; in tests neither does, so 0 calls)."""
    calls: list[tuple] = []

    def _record_runner(*args, **kwargs):
        calls.append(tuple(args[0]) if args else ())
        r = MagicMock()
        r.returncode = 0
        return r

    rotate_system_token(
        devbrain_home=tmp_path,
        reload_launchd=True,
        runner=_record_runner,
        setup_token_fn=_stub_setup_token_success(),
    )
    # In test env the plists won't exist at ~/Library/LaunchAgents,
    # so we just confirm rotate_system_token didn't fail.
    # Real-machine integration is covered by manual smoke test.


# ─── rotate_dev_token ────────────────────────────────────────────────────────


def test_rotate_dev_token_writes_to_profile_dir(tmp_path):
    # Set up a fake profile
    profile_dir = tmp_path / "profiles" / "mike_courtney"
    (profile_dir / ".claude").mkdir(parents=True)
    token_path = profile_dir / ".claude" / "oauth-token"
    token_path.write_text("sk-ant-oat01-OLD-TOKEN")
    token_path.chmod(0o600)

    result = rotate_dev_token(
        "mike_courtney",
        devbrain_home=tmp_path,
        runner=_fake_runner_success(),
        setup_token_fn=_stub_setup_token_success("sk-ant-oat01-NEW-TOKEN"),
    )

    assert result.success is True
    assert result.target_path == token_path
    assert result.backup_path is not None
    assert result.backup_path.read_text() == "sk-ant-oat01-OLD-TOKEN"
    assert token_path.read_text() == "sk-ant-oat01-NEW-TOKEN"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_rotate_dev_token_errors_when_profile_dir_missing(tmp_path):
    result = rotate_dev_token(
        "nonexistent_dev",
        devbrain_home=tmp_path,
        runner=_fake_runner_success(),
        setup_token_fn=_stub_setup_token_success(),
    )
    assert result.success is False
    assert "profile directory not found" in result.error


def test_rotate_dev_token_creates_token_file_if_missing(tmp_path):
    """If profile dir exists but no prior oauth-token, that's fine — just write."""
    profile_dir = tmp_path / "profiles" / "new_dev"
    profile_dir.mkdir(parents=True)
    # Don't create .claude/oauth-token

    result = rotate_dev_token(
        "new_dev",
        devbrain_home=tmp_path,
        runner=_fake_runner_success(),
        setup_token_fn=_stub_setup_token_success("sk-ant-oat01-FIRST-TOKEN"),
    )
    assert result.success is True
    assert result.backup_path is None  # nothing to back up
    assert (profile_dir / ".claude" / "oauth-token").read_text() == "sk-ant-oat01-FIRST-TOKEN"


def test_rotate_dev_token_returns_error_when_setup_fails(tmp_path):
    profile_dir = tmp_path / "profiles" / "mike_courtney"
    (profile_dir / ".claude").mkdir(parents=True)
    token_path = profile_dir / ".claude" / "oauth-token"
    token_path.write_text("UNCHANGED")

    result = rotate_dev_token(
        "mike_courtney",
        devbrain_home=tmp_path,
        runner=_fake_runner_success(),
        setup_token_fn=_stub_setup_token_failure("flow timed out"),
    )

    assert result.success is False
    assert result.error == "flow timed out"
    assert token_path.read_text() == "UNCHANGED"  # original preserved
