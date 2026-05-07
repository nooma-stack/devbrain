"""Tests for GeminiAdapter."""
from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ai_clis.gemini import (
    GeminiAdapter,
    _read_gemini_key_from_env_file,
    _stash_gemini_key,
)


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
        gemini_api_key="AIzaTestKey12345",
    )


def test_name():
    assert GeminiAdapter.name == "gemini"


# ─── spawn_args ──────────────────────────────────────────────────────────────

def test_spawn_args_sets_home(dev, tmp_path: Path):
    a = GeminiAdapter()
    spawn = a.spawn_args(dev, tmp_path)
    assert spawn.env["HOME"] == str(tmp_path)
    assert spawn.argv_prefix == ["gemini"]


def test_spawn_args_omits_api_key_when_not_set_anywhere(dev, tmp_path: Path):
    a = GeminiAdapter()
    spawn = a.spawn_args(dev, tmp_path)
    assert "GEMINI_API_KEY" not in spawn.env


def test_spawn_args_sets_api_key_from_dev_record(dev_with_api_key, tmp_path: Path):
    a = GeminiAdapter()
    spawn = a.spawn_args(dev_with_api_key, tmp_path)
    assert spawn.env["GEMINI_API_KEY"] == "AIzaTestKey12345"


def test_spawn_args_sets_api_key_from_profile_env_file(dev, tmp_path: Path):
    """Key stashed by `devbrain login` lands in <profile>/.devbrain/env;
    spawn_args should pick it up even when the dev record has no key."""
    _stash_gemini_key(tmp_path, "AIzaFromEnvFile")
    a = GeminiAdapter()
    spawn = a.spawn_args(dev, tmp_path)
    assert spawn.env["GEMINI_API_KEY"] == "AIzaFromEnvFile"


def test_spawn_args_sets_git_config_global(dev, tmp_path: Path):
    a = GeminiAdapter()
    spawn = a.spawn_args(dev, tmp_path)
    assert spawn.env["GIT_CONFIG_GLOBAL"] == str(tmp_path / ".gitconfig")


# ─── is_logged_in ────────────────────────────────────────────────────────────

def test_is_logged_in_true_with_dev_record_api_key(dev_with_api_key, tmp_path: Path):
    a = GeminiAdapter()
    assert a.is_logged_in(dev_with_api_key, tmp_path) is True


def test_is_logged_in_true_with_profile_env_file_key(dev, tmp_path: Path):
    _stash_gemini_key(tmp_path, "AIzaTestKey")
    a = GeminiAdapter()
    assert a.is_logged_in(dev, tmp_path) is True


def test_is_logged_in_false_when_no_key_anywhere(dev, tmp_path: Path):
    a = GeminiAdapter()
    assert a.is_logged_in(dev, tmp_path) is False


def test_is_logged_in_false_when_only_old_oauth_creds(dev, tmp_path: Path):
    """`google_accounts.json` is no longer the credential — env-file is."""
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini" / "google_accounts.json").write_text("{}")
    a = GeminiAdapter()
    assert a.is_logged_in(dev, tmp_path) is False


def test_required_dotfiles():
    a = GeminiAdapter()
    files = a.required_dotfiles()
    assert ".devbrain/env" in files
    assert ".gitconfig" in files


# ─── _stash_gemini_key + _read_gemini_key_from_env_file ──────────────────────

def test_stash_and_read_round_trip(tmp_path: Path):
    _stash_gemini_key(tmp_path, "AIzaRoundTripKey")
    assert _read_gemini_key_from_env_file(tmp_path) == "AIzaRoundTripKey"


def test_stash_creates_devbrain_dir(tmp_path: Path):
    _stash_gemini_key(tmp_path, "AIzaKey")
    assert (tmp_path / ".devbrain").is_dir()


def test_stash_file_is_mode_600(tmp_path: Path):
    _stash_gemini_key(tmp_path, "AIzaKey")
    env_file = tmp_path / ".devbrain" / "env"
    mode = oct(env_file.stat().st_mode)[-3:]
    assert mode == "600"


def test_stash_overwrites_existing_gemini_key(tmp_path: Path):
    _stash_gemini_key(tmp_path, "AIzaOldKey")
    _stash_gemini_key(tmp_path, "AIzaNewKey")
    assert _read_gemini_key_from_env_file(tmp_path) == "AIzaNewKey"


def test_stash_preserves_other_env_vars(tmp_path: Path):
    """Other KEY=VALUE lines in .devbrain/env shouldn't be clobbered."""
    env_dir = tmp_path / ".devbrain"
    env_dir.mkdir()
    (env_dir / "env").write_text("OTHER_VAR=preserved\nGEMINI_API_KEY=AIzaOld\n")
    _stash_gemini_key(tmp_path, "AIzaNew")
    content = (env_dir / "env").read_text()
    assert "OTHER_VAR=preserved" in content
    assert "GEMINI_API_KEY=AIzaNew" in content
    assert "GEMINI_API_KEY=AIzaOld" not in content


def test_read_returns_none_when_file_missing(tmp_path: Path):
    assert _read_gemini_key_from_env_file(tmp_path) is None


# ─── login() ─────────────────────────────────────────────────────────────────

def test_login_with_api_key_on_dev_record_skips_prompt(
    dev_with_api_key, tmp_path: Path
):
    a = GeminiAdapter()
    with patch("ai_clis.gemini._prompt_for_api_key") as mock_prompt:
        result = a.login(dev_with_api_key, tmp_path)
    assert result.success is True
    mock_prompt.assert_not_called()


def test_login_prompts_and_stashes_when_no_dev_record_key(dev, tmp_path: Path):
    a = GeminiAdapter()
    with patch("ai_clis.gemini._prompt_for_api_key", return_value="AIzaPastedByDev"):
        result = a.login(dev, tmp_path)
    assert result.success is True
    assert _read_gemini_key_from_env_file(tmp_path) == "AIzaPastedByDev"


def test_login_rejects_empty_input(dev, tmp_path: Path):
    a = GeminiAdapter()
    with patch("ai_clis.gemini._prompt_for_api_key", return_value=""):
        result = a.login(dev, tmp_path)
    assert result.success is False
    assert "no API key" in result.error
    assert _read_gemini_key_from_env_file(tmp_path) is None


def test_login_rejects_key_without_aiza_prefix(dev, tmp_path: Path):
    a = GeminiAdapter()
    with patch("ai_clis.gemini._prompt_for_api_key", return_value="not-a-real-key"):
        result = a.login(dev, tmp_path)
    assert result.success is False
    assert "AIza" in result.error or "doesn't look like" in result.error
    assert _read_gemini_key_from_env_file(tmp_path) is None


def test_login_writes_mode_600_env_file(dev, tmp_path: Path):
    a = GeminiAdapter()
    with patch("ai_clis.gemini._prompt_for_api_key", return_value="AIzaSecure"):
        a.login(dev, tmp_path)
    env_file = tmp_path / ".devbrain" / "env"
    mode = oct(env_file.stat().st_mode)[-3:]
    assert mode == "600"


def test_prompt_reads_from_provided_streams(tmp_path: Path):
    """Decoupling streams lets tests drive the prompt without monkeypatching
    sys.stdin globally."""
    from ai_clis.gemini import _prompt_for_api_key
    prompt_in = io.StringIO("AIzaFromStream\n")
    prompt_out = io.StringIO()
    result = _prompt_for_api_key(prompt_in=prompt_in, prompt_out=prompt_out)
    assert result == "AIzaFromStream"
    assert "API key" in prompt_out.getvalue()
