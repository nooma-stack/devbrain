"""Tests for CodexAdapter."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ai_clis.codex import CodexAdapter
from ai_clis.base import LoginResult, SpawnArgs


@pytest.fixture
def dev():
    return SimpleNamespace(
        dev_id="alice",
        full_name="Alice Smith",
        email="alice@example.com",
    )


def test_name():
    assert CodexAdapter.name == "codex"


def test_spawn_args_sets_codex_home(dev, tmp_path: Path):
    a = CodexAdapter()
    spawn = a.spawn_args(dev, tmp_path)
    assert isinstance(spawn, SpawnArgs)
    assert spawn.env["CODEX_HOME"] == str(tmp_path / ".codex")
    assert spawn.argv_prefix == ["codex"]


def test_spawn_args_sets_git_config_global(dev, tmp_path: Path):
    a = CodexAdapter()
    spawn = a.spawn_args(dev, tmp_path)
    assert spawn.env["GIT_CONFIG_GLOBAL"] == str(tmp_path / ".gitconfig")


def test_spawn_args_sets_git_author(dev, tmp_path: Path):
    a = CodexAdapter()
    spawn = a.spawn_args(dev, tmp_path)
    assert spawn.env["GIT_AUTHOR_NAME"] == "Alice Smith"
    assert spawn.env["GIT_AUTHOR_EMAIL"] == "alice@example.com"


def test_spawn_args_does_not_set_home(dev, tmp_path: Path):
    """Codex uses CODEX_HOME — HOME stays untouched (precise, no blast radius)."""
    a = CodexAdapter()
    spawn = a.spawn_args(dev, tmp_path)
    assert "HOME" not in spawn.env


def test_is_logged_in_false_when_no_auth(dev, tmp_path: Path):
    a = CodexAdapter()
    assert a.is_logged_in(dev, tmp_path) is False


def test_is_logged_in_true_when_auth_json_present(dev, tmp_path: Path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text("{}")
    a = CodexAdapter()
    assert a.is_logged_in(dev, tmp_path) is True


def test_required_dotfiles():
    a = CodexAdapter()
    files = a.required_dotfiles()
    assert ".codex/auth.json" in files
    assert ".gitconfig" in files


def test_login_invokes_subprocess_with_device_auth(dev, tmp_path: Path):
    a = CodexAdapter()
    mock_run_result = MagicMock(returncode=0)
    with patch("ai_clis.codex.subprocess.run", return_value=mock_run_result) as mock_run, \
         patch.object(CodexAdapter, "is_logged_in", return_value=True):
        result = a.login(dev, tmp_path)

    assert result.success is True
    args, kwargs = mock_run.call_args
    assert args[0] == ["codex", "login", "--device-auth"]
    assert kwargs["env"]["CODEX_HOME"] == str(tmp_path / ".codex")


def test_login_handles_missing_cli(dev, tmp_path: Path):
    a = CodexAdapter()
    with patch("ai_clis.codex.subprocess.run", side_effect=FileNotFoundError()):
        result = a.login(dev, tmp_path)
    assert result.success is False
    assert "not found" in result.error


def test_login_returns_failure_on_nonzero_exit(dev, tmp_path: Path):
    a = CodexAdapter()
    with patch("ai_clis.codex.subprocess.run", return_value=MagicMock(returncode=1)):
        result = a.login(dev, tmp_path)
    assert result.success is False
    assert "exited with code 1" in result.error


def test_login_returns_failure_when_auth_not_persisted(dev, tmp_path: Path):
    a = CodexAdapter()
    with patch("ai_clis.codex.subprocess.run", return_value=MagicMock(returncode=0)), \
         patch.object(CodexAdapter, "is_logged_in", return_value=False):
        result = a.login(dev, tmp_path)
    assert result.success is False
    assert "auth.json not found" in result.error
