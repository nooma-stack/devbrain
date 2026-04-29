"""Tests for ClaudeAdapter."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ai_clis.claude import ClaudeAdapter
from ai_clis.base import SpawnArgs


@pytest.fixture
def dev():
    return SimpleNamespace(
        dev_id="alice",
        full_name="Alice Smith",
        email="alice@example.com",
    )


def test_name():
    assert ClaudeAdapter.name == "claude"


def test_spawn_args_sets_home(dev, tmp_path: Path):
    """Claude has no CLAUDE_CONFIG_DIR — HOME-swap is the only mechanism."""
    a = ClaudeAdapter()
    spawn = a.spawn_args(dev, tmp_path)
    assert isinstance(spawn, SpawnArgs)
    assert spawn.env["HOME"] == str(tmp_path)
    assert spawn.argv_prefix == ["claude"]


def test_spawn_args_sets_git_config_global(dev, tmp_path: Path):
    a = ClaudeAdapter()
    spawn = a.spawn_args(dev, tmp_path)
    assert spawn.env["GIT_CONFIG_GLOBAL"] == str(tmp_path / ".gitconfig")


def test_spawn_args_sets_git_author(dev, tmp_path: Path):
    a = ClaudeAdapter()
    spawn = a.spawn_args(dev, tmp_path)
    assert spawn.env["GIT_AUTHOR_NAME"] == "Alice Smith"
    assert spawn.env["GIT_AUTHOR_EMAIL"] == "alice@example.com"


def test_is_logged_in_false_when_no_creds(dev, tmp_path: Path):
    a = ClaudeAdapter()
    assert a.is_logged_in(dev, tmp_path) is False


def test_is_logged_in_true_when_claude_json_present(dev, tmp_path: Path):
    (tmp_path / ".claude.json").write_text("{}")
    a = ClaudeAdapter()
    assert a.is_logged_in(dev, tmp_path) is True


def test_required_dotfiles():
    a = ClaudeAdapter()
    files = a.required_dotfiles()
    assert ".claude.json" in files
    assert ".gitconfig" in files


def test_login_invokes_claude_auth_login_with_swapped_home(dev, tmp_path: Path):
    a = ClaudeAdapter()
    with patch("ai_clis.claude.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run, \
         patch.object(ClaudeAdapter, "is_logged_in", return_value=True):
        result = a.login(dev, tmp_path)

    assert result.success is True
    args, kwargs = mock_run.call_args
    assert args[0] == ["claude", "auth", "login"]
    assert kwargs["env"]["HOME"] == str(tmp_path)


def test_login_handles_missing_cli(dev, tmp_path: Path):
    a = ClaudeAdapter()
    with patch("ai_clis.claude.subprocess.run", side_effect=FileNotFoundError()):
        result = a.login(dev, tmp_path)
    assert result.success is False
    assert "not found" in result.error


def test_login_returns_failure_on_nonzero_exit(dev, tmp_path: Path):
    a = ClaudeAdapter()
    with patch("ai_clis.claude.subprocess.run", return_value=MagicMock(returncode=1)):
        result = a.login(dev, tmp_path)
    assert result.success is False


def test_login_returns_failure_when_creds_not_persisted(dev, tmp_path: Path):
    a = ClaudeAdapter()
    with patch("ai_clis.claude.subprocess.run", return_value=MagicMock(returncode=0)), \
         patch.object(ClaudeAdapter, "is_logged_in", return_value=False):
        result = a.login(dev, tmp_path)
    assert result.success is False
    assert "not written" in result.error
