"""Tests for the click commands `login`, `logins`, `logout` in cli.py."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

import cli as cli_module
import dev_login
import profiles
from ai_clis.base import AdapterRegistry, AICliAdapter, LoginResult, SpawnArgs


class _StubAdapter(AICliAdapter):
    name = "stub"
    oauth_callback_ports: list[int] = []

    def spawn_args(self, dev, profile_dir):
        return SpawnArgs()

    def login(self, dev, profile_dir):
        (profile_dir / ".stub").mkdir(exist_ok=True)
        (profile_dir / ".stub" / "auth.json").write_text("{}")
        return LoginResult(success=True)

    def is_logged_in(self, dev, profile_dir):
        return (profile_dir / ".stub" / "auth.json").exists()

    def required_dotfiles(self):
        return [".stub/"]


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
    monkeypatch.setattr(dev_login, "default_registry", reg)
    yield reg


@pytest.fixture
def fake_db(monkeypatch):
    db = MagicMock()
    db.get_dev.return_value = {
        "dev_id": "alice",
        "full_name": "Alice Smith",
        "email": "alice@example.com",
    }
    monkeypatch.setattr(cli_module, "get_db", lambda: db)
    yield db


def test_login_command_succeeds(isolated_root, stub_registry, fake_db, monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    runner = CliRunner()
    result = runner.invoke(cli_module.cli, ["login", "--dev", "alice", "--cli", "stub"])
    assert result.exit_code == 0, result.output
    assert "✅" in result.output
    assert "alice" in result.output


def test_login_command_validates_dev_id(isolated_root, stub_registry, fake_db):
    runner = CliRunner()
    result = runner.invoke(
        cli_module.cli, ["login", "--dev", "../etc", "--cli", "stub"],
    )
    assert result.exit_code != 0


def test_login_command_uses_supplied_git_identity(
    isolated_root, stub_registry, fake_db, monkeypatch,
):
    monkeypatch.delenv("TMUX", raising=False)
    runner = CliRunner()
    result = runner.invoke(
        cli_module.cli,
        ["login", "--dev", "alice", "--cli", "stub",
         "--git-name", "Override", "--git-email", "o@x.com"],
    )
    assert result.exit_code == 0, result.output
    contents = (isolated_root / "alice" / ".gitconfig").read_text()
    assert "Override" in contents
    assert "o@x.com" in contents


def test_logins_command_empty(isolated_root, stub_registry, fake_db):
    runner = CliRunner()
    result = runner.invoke(cli_module.cli, ["logins"])
    assert result.exit_code == 0
    assert "No profiles" in result.output


def test_logins_command_shows_dev_x_cli_table(
    isolated_root, stub_registry, fake_db, monkeypatch,
):
    monkeypatch.delenv("TMUX", raising=False)
    runner = CliRunner()
    runner.invoke(cli_module.cli, ["login", "--dev", "alice", "--cli", "stub"])
    result = runner.invoke(cli_module.cli, ["logins"])
    assert result.exit_code == 0
    assert "dev_id" in result.output
    assert "stub" in result.output
    assert "alice" in result.output
    assert "✅" in result.output


def test_logout_command_full_profile(
    isolated_root, stub_registry, fake_db, monkeypatch,
):
    monkeypatch.delenv("TMUX", raising=False)
    runner = CliRunner()
    runner.invoke(cli_module.cli, ["login", "--dev", "alice", "--cli", "stub"])
    assert (isolated_root / "alice").exists()

    result = runner.invoke(cli_module.cli, ["logout", "--dev", "alice", "--yes"])
    assert result.exit_code == 0, result.output
    assert not (isolated_root / "alice").exists()


def test_logout_command_per_cli(isolated_root, stub_registry, fake_db, monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    runner = CliRunner()
    runner.invoke(cli_module.cli, ["login", "--dev", "alice", "--cli", "stub"])
    profile = isolated_root / "alice"
    assert (profile / ".stub").exists()
    assert (profile / ".gitconfig").exists()

    result = runner.invoke(
        cli_module.cli, ["logout", "--dev", "alice", "--cli", "stub", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert not (profile / ".stub").exists()
    assert profile.exists()
    assert (profile / ".gitconfig").exists()


def test_logout_command_requires_confirmation_or_yes(
    isolated_root, stub_registry, fake_db, monkeypatch,
):
    monkeypatch.delenv("TMUX", raising=False)
    runner = CliRunner()
    runner.invoke(cli_module.cli, ["login", "--dev", "alice", "--cli", "stub"])

    # Without --yes, expects confirmation. We pipe "n" → abort
    result = runner.invoke(
        cli_module.cli, ["logout", "--dev", "alice"], input="n\n",
    )
    assert result.exit_code != 0  # aborted
    assert (isolated_root / "alice").exists()
