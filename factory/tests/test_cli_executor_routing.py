"""Tests for cli_executor.run_cli adapter routing (Phase 4).

Verifies that when a dev_id is resolvable (explicit or via DEVBRAIN_DEV_ID env),
run_cli applies the adapter's spawn_args env on top of os.environ, with
caller-supplied env_override taking final precedence.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import cli_executor
import profiles
from ai_clis.base import AdapterRegistry, AICliAdapter, LoginResult, SpawnArgs


class _StubAdapter(AICliAdapter):
    name = "claude"  # use a real name so existing CLI_CONFIGS keys match
    oauth_callback_ports: list[int] = []

    def spawn_args(self, dev, profile_dir):
        return SpawnArgs(
            env={
                "ADAPTER_MARKER": "yes",
                "DEV_ID": dev.dev_id,
                "PROFILE_DIR": str(profile_dir),
            },
            argv_prefix=["claude"],
        )

    def login(self, dev, profile_dir):
        return LoginResult(success=True)

    def is_logged_in(self, dev, profile_dir):
        return True

    def required_dotfiles(self):
        return [".claude/"]


@pytest.fixture
def isolated_root(tmp_path: Path, monkeypatch):
    fake_root = tmp_path / "profiles-root"
    fake_root.mkdir()
    monkeypatch.setattr(profiles, "_PROFILES_ROOT_OVERRIDE", fake_root, raising=False)
    yield fake_root


@pytest.fixture
def stub_registry(monkeypatch):
    reg = AdapterRegistry()
    reg.register(_StubAdapter)
    monkeypatch.setattr(cli_executor, "_default_registry", reg, raising=False)
    yield reg


def _make_subprocess_mock(stdout="ok", returncode=0):
    return MagicMock(returncode=returncode, stdout=stdout, stderr="")


def test_run_cli_without_dev_id_falls_back_to_existing_behavior(
    isolated_root, stub_registry, monkeypatch,
):
    """No dev_id, no DEVBRAIN_DEV_ID env → no adapter env layered."""
    monkeypatch.delenv("DEVBRAIN_DEV_ID", raising=False)
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return _make_subprocess_mock()

    with patch("cli_executor.subprocess.run", side_effect=fake_run), \
         patch("cli_executor.is_cli_available", return_value=True):
        cli_executor.run_cli("claude", "hello")

    assert "ADAPTER_MARKER" not in captured["env"]


def test_run_cli_with_explicit_dev_id_applies_adapter_env(
    isolated_root, stub_registry, monkeypatch,
):
    monkeypatch.delenv("DEVBRAIN_DEV_ID", raising=False)
    profiles.get_profile_dir("alice")  # ensure profile exists

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return _make_subprocess_mock()

    with patch("cli_executor.subprocess.run", side_effect=fake_run), \
         patch("cli_executor.is_cli_available", return_value=True):
        cli_executor.run_cli("claude", "hello", dev_id="alice")

    assert captured["env"].get("ADAPTER_MARKER") == "yes"
    assert captured["env"].get("DEV_ID") == "alice"


def test_run_cli_uses_devbrain_dev_id_env_when_no_explicit_arg(
    isolated_root, stub_registry, monkeypatch,
):
    monkeypatch.setenv("DEVBRAIN_DEV_ID", "alice")
    profiles.get_profile_dir("alice")

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return _make_subprocess_mock()

    with patch("cli_executor.subprocess.run", side_effect=fake_run), \
         patch("cli_executor.is_cli_available", return_value=True):
        cli_executor.run_cli("claude", "hello")

    assert captured["env"].get("ADAPTER_MARKER") == "yes"
    assert captured["env"].get("DEV_ID") == "alice"


def test_run_cli_explicit_arg_wins_over_env(
    isolated_root, stub_registry, monkeypatch,
):
    monkeypatch.setenv("DEVBRAIN_DEV_ID", "bob")
    profiles.get_profile_dir("alice")

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return _make_subprocess_mock()

    with patch("cli_executor.subprocess.run", side_effect=fake_run), \
         patch("cli_executor.is_cli_available", return_value=True):
        cli_executor.run_cli("claude", "hello", dev_id="alice")

    assert captured["env"]["DEV_ID"] == "alice"


def test_run_cli_caller_env_override_wins_over_adapter_env(
    isolated_root, stub_registry, monkeypatch,
):
    monkeypatch.setenv("DEVBRAIN_DEV_ID", "alice")
    profiles.get_profile_dir("alice")

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return _make_subprocess_mock()

    with patch("cli_executor.subprocess.run", side_effect=fake_run), \
         patch("cli_executor.is_cli_available", return_value=True):
        cli_executor.run_cli(
            "claude", "hello",
            env_override={"ADAPTER_MARKER": "overridden"},
        )

    assert captured["env"]["ADAPTER_MARKER"] == "overridden"


def test_run_cli_unknown_adapter_falls_through_silently(
    isolated_root, stub_registry, monkeypatch,
):
    """If cli_name has no registered adapter, run_cli still functions (existing behavior)."""
    monkeypatch.setenv("DEVBRAIN_DEV_ID", "alice")
    profiles.get_profile_dir("alice")

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return _make_subprocess_mock()

    # CLI_CONFIGS has gemini (real entry); registry has only `claude` stub
    with patch("cli_executor.subprocess.run", side_effect=fake_run), \
         patch("cli_executor.is_cli_available", return_value=True):
        cli_executor.run_cli("gemini", "hello")

    assert "ADAPTER_MARKER" not in captured["env"]


def test_run_cli_invalid_dev_id_falls_through(
    isolated_root, stub_registry, monkeypatch,
):
    """Invalid dev_id (path traversal etc.) falls back to non-adapter path silently."""
    monkeypatch.delenv("DEVBRAIN_DEV_ID", raising=False)

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return _make_subprocess_mock()

    with patch("cli_executor.subprocess.run", side_effect=fake_run), \
         patch("cli_executor.is_cli_available", return_value=True):
        result = cli_executor.run_cli("claude", "hello", dev_id="../etc/passwd")

    # Subprocess still ran; adapter wasn't applied
    assert "ADAPTER_MARKER" not in captured["env"]
    assert result.success is True
