"""Tests for ClaudeAdapter."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ai_clis.claude import (
    ClaudeAdapter,
    _extract_oauth_token,
    _plant_fakebin,
)
from ai_clis.base import SpawnArgs


@pytest.fixture
def dev():
    return SimpleNamespace(
        dev_id="alice",
        full_name="Alice Smith",
        email="alice@example.com",
    )


# ─── Adapter identity / spawn ────────────────────────────────────────────────

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


# ─── is_logged_in / required_dotfiles ────────────────────────────────────────

def test_is_logged_in_false_when_no_oauth_token(dev, tmp_path: Path):
    a = ClaudeAdapter()
    assert a.is_logged_in(dev, tmp_path) is False


def test_is_logged_in_true_when_oauth_token_present(dev, tmp_path: Path):
    (tmp_path / ".claude").mkdir(exist_ok=True)
    (tmp_path / ".claude" / "oauth-token").write_text("sk-ant-oat01-test")
    a = ClaudeAdapter()
    assert a.is_logged_in(dev, tmp_path) is True


def test_is_logged_in_false_when_only_claude_json_present(dev, tmp_path: Path):
    """`.claude.json` alone (no `oauth-token`) is not enough — claude setup-token
    creates `.claude.json` as a side effect even on failed flows."""
    (tmp_path / ".claude.json").write_text("{}")
    a = ClaudeAdapter()
    assert a.is_logged_in(dev, tmp_path) is False


def test_required_dotfiles():
    a = ClaudeAdapter()
    files = a.required_dotfiles()
    assert ".claude/oauth-token" in files
    assert ".gitconfig" in files


# ─── _extract_oauth_token helper ─────────────────────────────────────────────

def test_extract_oauth_token_finds_token():
    text = "Welcome\n...\nsk-ant-oat01-AbCdEf1234567890_-Fake\n...\n"
    assert _extract_oauth_token(text) == "sk-ant-oat01-AbCdEf1234567890_-Fake"


def test_extract_oauth_token_returns_none_when_absent():
    text = "No token here, just other stuff\n"
    assert _extract_oauth_token(text) is None


def test_extract_oauth_token_handles_ansi_escapes():
    """Captured TTY output has escape sequences interleaved with content."""
    text = "\x1b[2m...\x1b[0m\nsk-ant-oat01-tok_with_dashes-and_underscores\n\x1b[?25h"
    assert _extract_oauth_token(text) == "sk-ant-oat01-tok_with_dashes-and_underscores"


def test_extract_oauth_token_picks_first_match_when_multiple():
    text = "first sk-ant-oat01-FIRST then sk-ant-oat01-SECOND"
    assert _extract_oauth_token(text) == "sk-ant-oat01-FIRST"


# ─── _plant_fakebin helper ───────────────────────────────────────────────────

def test_plant_fakebin_creates_open_and_xdg_open(tmp_path: Path):
    fakebin = _plant_fakebin(tmp_path)
    assert fakebin == tmp_path / ".claude" / ".devbrain-fakebin"
    assert (fakebin / "open").exists()
    assert (fakebin / "xdg-open").exists()


def test_plant_fakebin_files_are_executable(tmp_path: Path):
    fakebin = _plant_fakebin(tmp_path)
    for cmd in ("open", "xdg-open"):
        mode = (fakebin / cmd).stat().st_mode & 0o777
        assert mode & 0o100, f"{cmd} should be user-executable (got mode {oct(mode)})"


def test_plant_fakebin_files_exit_nonzero(tmp_path: Path):
    """The shim binaries must fail when invoked, so claude falls back to
    print-URL instead of "successfully" launching a (non-existent) browser."""
    import subprocess as sp
    fakebin = _plant_fakebin(tmp_path)
    for cmd in ("open", "xdg-open"):
        result = sp.run([str(fakebin / cmd), "https://example.com"], capture_output=True)
        assert result.returncode != 0


def test_plant_fakebin_idempotent(tmp_path: Path):
    """Calling twice doesn't error and both files still exist."""
    _plant_fakebin(tmp_path)
    fakebin = _plant_fakebin(tmp_path)
    assert (fakebin / "open").exists()
    assert (fakebin / "xdg-open").exists()


# ─── login() flow ────────────────────────────────────────────────────────────

def _fake_script_run_with_token(token: str = "sk-ant-oat01-FakeT0ken_For-Testing"):
    """Side-effect for subprocess.run that simulates script(1) capturing
    claude setup-token's output (including the printed token) into the
    log file path passed as cmd[2]."""

    def side_effect(*args, **kwargs):
        cmd = args[0]
        if cmd and cmd[0] == "script":
            log_path = Path(cmd[2])
            log_path.write_text(
                "Welcome to Claude Code v2.1.132\n"
                "Opening browser to sign in...\n"
                "Browser didn't open? Use the url below to sign in (c to copy)\n"
                "https://claude.com/cai/oauth/authorize?code=true&...\n"
                "Paste code here if prompted >\n"
                f"{token}\n"
            )
            return MagicMock(returncode=0)
        # security keychain calls — pass through as success
        return MagicMock(returncode=0, stderr=b"")

    return side_effect


@patch("ai_clis.claude._ensure_keychain")
@patch("ai_clis.claude.subprocess.run")
def test_login_invokes_setup_token_via_script(
    mock_run, mock_keychain, dev, tmp_path: Path
):
    mock_run.side_effect = _fake_script_run_with_token()
    a = ClaudeAdapter()
    result = a.login(dev, tmp_path)

    assert result.success is True

    # Find the script call (keychain may not be called since it's patched)
    script_calls = [c for c in mock_run.call_args_list if c.args[0][0] == "script"]
    assert len(script_calls) == 1, f"expected one script call, got {script_calls}"
    cmd = script_calls[0].args[0]
    assert cmd[0:2] == ["script", "-q"]
    assert cmd[3:] == ["claude", "setup-token"]


@patch("ai_clis.claude._ensure_keychain")
@patch("ai_clis.claude.subprocess.run")
def test_login_swaps_home_and_prepends_fakebin_to_path(
    mock_run, mock_keychain, dev, tmp_path: Path
):
    mock_run.side_effect = _fake_script_run_with_token()
    a = ClaudeAdapter()
    result = a.login(dev, tmp_path)

    assert result.success is True
    script_calls = [c for c in mock_run.call_args_list if c.args[0][0] == "script"]
    env = script_calls[0].kwargs["env"]
    assert env["HOME"] == str(tmp_path)
    fakebin = str(tmp_path / ".claude" / ".devbrain-fakebin")
    assert env["PATH"].startswith(fakebin + os.pathsep)


@patch("ai_clis.claude._ensure_keychain")
@patch("ai_clis.claude.subprocess.run")
def test_login_writes_oauth_token_file_with_mode_600(
    mock_run, mock_keychain, dev, tmp_path: Path
):
    mock_run.side_effect = _fake_script_run_with_token("sk-ant-oat01-MyTestToken123")
    a = ClaudeAdapter()
    result = a.login(dev, tmp_path)

    assert result.success is True
    token_file = tmp_path / ".claude" / "oauth-token"
    assert token_file.exists()
    assert token_file.read_text() == "sk-ant-oat01-MyTestToken123"
    mode = oct(token_file.stat().st_mode)[-3:]
    assert mode == "600", f"oauth-token should be mode 600, got {mode}"


@patch("ai_clis.claude._ensure_keychain")
@patch("ai_clis.claude.subprocess.run")
def test_login_unlinks_script_log_after_token_extracted(
    mock_run, mock_keychain, dev, tmp_path: Path
):
    """The transient script(1) log captures the token mid-flight; it must
    be unlinked once the token is stashed at oauth-token (so the only
    persistent location is the mode-600 file)."""
    mock_run.side_effect = _fake_script_run_with_token()
    a = ClaudeAdapter()
    a.login(dev, tmp_path)

    leftover_logs = list((tmp_path / ".claude").glob(".setup-token-*.log"))
    assert leftover_logs == [], f"script log not cleaned up: {leftover_logs}"


@patch("ai_clis.claude._ensure_keychain")
@patch("ai_clis.claude.subprocess.run")
def test_login_returns_failure_on_nonzero_exit(
    mock_run, mock_keychain, dev, tmp_path: Path
):
    mock_run.return_value = MagicMock(returncode=1)
    a = ClaudeAdapter()
    result = a.login(dev, tmp_path)
    assert result.success is False
    assert "exited with code 1" in result.error


@patch("ai_clis.claude._ensure_keychain")
@patch("ai_clis.claude.subprocess.run")
def test_login_returns_failure_when_token_not_in_log(
    mock_run, mock_keychain, dev, tmp_path: Path
):
    """If claude exits 0 but no sk-ant-oat01-... in captured output, fail."""
    def no_token_run(*args, **kwargs):
        cmd = args[0]
        if cmd[0] == "script":
            Path(cmd[2]).write_text("Some output but no token here.\n")
            return MagicMock(returncode=0)
        return MagicMock(returncode=0, stderr=b"")
    mock_run.side_effect = no_token_run

    a = ClaudeAdapter()
    result = a.login(dev, tmp_path)
    assert result.success is False
    assert "no sk-ant-oat01" in result.error


@patch("ai_clis.claude._ensure_keychain")
@patch("ai_clis.claude.subprocess.run")
def test_login_handles_missing_binary(
    mock_run, mock_keychain, dev, tmp_path: Path
):
    mock_run.side_effect = FileNotFoundError(2, "No such file", "script")
    a = ClaudeAdapter()
    result = a.login(dev, tmp_path)
    assert result.success is False
    assert "binary not found" in result.error


@patch("ai_clis.claude.subprocess.run")
def test_login_returns_failure_when_keychain_provisioning_fails(
    mock_run, dev, tmp_path: Path
):
    """Keychain failure short-circuits before claude setup-token runs."""
    import subprocess as sp
    err = sp.CalledProcessError(1, ["security"], stderr=b"keychain error")
    with patch("ai_clis.claude._ensure_keychain", side_effect=err):
        a = ClaudeAdapter()
        result = a.login(dev, tmp_path)
    assert result.success is False
    assert "keychain" in result.error.lower()
    # Should not have invoked subprocess.run for the script call
    script_calls = [
        c for c in mock_run.call_args_list
        if c.args and c.args[0] and c.args[0][0] == "script"
    ]
    assert script_calls == []
