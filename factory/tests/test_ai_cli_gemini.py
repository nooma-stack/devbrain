"""Tests for GeminiAdapter."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ai_clis.gemini import GeminiAdapter


@pytest.fixture
def dev():
    return SimpleNamespace(
        dev_id="alice",
        full_name="Alice Smith",
        email="alice@example.com",
        gemini_api_key=None,
    )


@pytest.fixture
def dev_with_api_key():
    return SimpleNamespace(
        dev_id="bob",
        full_name="Bob Jones",
        email="bob@example.com",
        gemini_api_key="test-api-key-12345",
    )


def test_name():
    assert GeminiAdapter.name == "gemini"


def test_spawn_args_sets_home(dev, tmp_path: Path):
    a = GeminiAdapter()
    spawn = a.spawn_args(dev, tmp_path)
    assert spawn.env["HOME"] == str(tmp_path)
    assert spawn.argv_prefix == ["gemini"]


def test_spawn_args_omits_api_key_when_not_set(dev, tmp_path: Path):
    a = GeminiAdapter()
    spawn = a.spawn_args(dev, tmp_path)
    assert "GEMINI_API_KEY" not in spawn.env


def test_spawn_args_sets_api_key_when_present(dev_with_api_key, tmp_path: Path):
    a = GeminiAdapter()
    spawn = a.spawn_args(dev_with_api_key, tmp_path)
    assert spawn.env["GEMINI_API_KEY"] == "test-api-key-12345"


def test_spawn_args_sets_git_config_global(dev, tmp_path: Path):
    a = GeminiAdapter()
    spawn = a.spawn_args(dev, tmp_path)
    assert spawn.env["GIT_CONFIG_GLOBAL"] == str(tmp_path / ".gitconfig")


def test_is_logged_in_true_with_api_key(dev_with_api_key, tmp_path: Path):
    a = GeminiAdapter()
    assert a.is_logged_in(dev_with_api_key, tmp_path) is True


def test_is_logged_in_true_with_oauth_creds(dev, tmp_path: Path):
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini" / "google_accounts.json").write_text("{}")
    a = GeminiAdapter()
    assert a.is_logged_in(dev, tmp_path) is True


def test_is_logged_in_false_when_neither_present(dev, tmp_path: Path):
    a = GeminiAdapter()
    assert a.is_logged_in(dev, tmp_path) is False


def test_required_dotfiles():
    a = GeminiAdapter()
    files = a.required_dotfiles()
    assert ".gemini/" in files
    assert ".gitconfig" in files


def test_login_with_api_key_skips_oauth(dev_with_api_key, tmp_path: Path):
    a = GeminiAdapter()
    with patch("ai_clis.gemini.subprocess.run") as mock_run:
        result = a.login(dev_with_api_key, tmp_path)
    assert result.success is True
    assert "API_KEY" in result.hint
    mock_run.assert_not_called()


def test_login_without_api_key_runs_gemini(dev, tmp_path: Path):
    a = GeminiAdapter()
    with patch("ai_clis.gemini.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run, \
         patch.object(GeminiAdapter, "is_logged_in", return_value=True):
        result = a.login(dev, tmp_path)
    assert result.success is True
    args, kwargs = mock_run.call_args
    assert args[0] == ["gemini"]
    assert kwargs["env"]["HOME"] == str(tmp_path)


def test_login_handles_missing_cli(dev, tmp_path: Path):
    a = GeminiAdapter()
    with patch("ai_clis.gemini.subprocess.run", side_effect=FileNotFoundError()):
        result = a.login(dev, tmp_path)
    assert result.success is False
    assert "not found" in result.error


def test_login_returns_failure_on_nonzero_exit(dev, tmp_path: Path):
    a = GeminiAdapter()
    with patch("ai_clis.gemini.subprocess.run", return_value=MagicMock(returncode=1)):
        result = a.login(dev, tmp_path)
    assert result.success is False
