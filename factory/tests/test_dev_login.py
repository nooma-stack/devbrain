"""Tests for factory.dev_login business logic."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import dev_login
import profiles
from ai_clis.base import AdapterRegistry, AICliAdapter, LoginResult, SpawnArgs


class _StubAdapter(AICliAdapter):
    name = "stub"
    oauth_callback_ports: list[int] = []

    login_should_succeed = True

    def spawn_args(self, dev, profile_dir):
        return SpawnArgs()

    def login(self, dev, profile_dir):
        if self.login_should_succeed:
            (profile_dir / ".stub").mkdir(exist_ok=True)
            (profile_dir / ".stub" / "auth.json").write_text("{}")
            return LoginResult(success=True)
        return LoginResult(success=False, error="stub failure", hint="retry")

    def is_logged_in(self, dev, profile_dir):
        return (profile_dir / ".stub" / "auth.json").exists()

    def required_dotfiles(self):
        return [".stub/", ".gitconfig"]


@pytest.fixture
def fake_db():
    db = MagicMock()
    db.get_dev.return_value = {
        "dev_id": "alice",
        "full_name": "Alice Smith",
        "email": "alice@example.com",
    }
    return db


@pytest.fixture
def isolated_root(tmp_path: Path, monkeypatch):
    fake_root = tmp_path / "profiles-root"
    fake_root.mkdir()
    monkeypatch.setattr(profiles, "_PROFILES_ROOT_OVERRIDE", fake_root, raising=False)
    yield fake_root


@pytest.fixture
def stub_registry(monkeypatch):
    """Replace default_registry with a fresh registry containing only StubAdapter."""
    reg = AdapterRegistry()
    reg.register(_StubAdapter)
    monkeypatch.setattr(dev_login, "default_registry", reg)
    yield reg


def test_login_dev_creates_profile(isolated_root, stub_registry, fake_db):
    outcomes = dev_login.login_dev("alice", ["stub"], db=fake_db, set_tmux_env=False)
    assert len(outcomes) == 1
    assert outcomes[0].success is True
    assert (isolated_root / "alice").is_dir()


def test_login_dev_writes_gitconfig_from_dev_record(isolated_root, stub_registry, fake_db):
    dev_login.login_dev("alice", ["stub"], db=fake_db, set_tmux_env=False)
    contents = (isolated_root / "alice" / ".gitconfig").read_text()
    assert "Alice Smith" in contents
    assert "alice@example.com" in contents


def test_login_dev_uses_supplied_identity(isolated_root, stub_registry, fake_db):
    dev_login.login_dev(
        "alice", ["stub"],
        db=fake_db,
        git_name="Override Name",
        git_email="override@x.com",
        set_tmux_env=False,
    )
    contents = (isolated_root / "alice" / ".gitconfig").read_text()
    assert "Override Name" in contents
    assert "override@x.com" in contents


def test_login_dev_calls_prompt_when_no_dev_record(isolated_root, stub_registry):
    db = MagicMock()
    db.get_dev.return_value = None
    prompted = []

    def prompt():
        prompted.append("called")
        return ("Prompt Name", "prompt@x.com")

    dev_login.login_dev(
        "newdev", ["stub"], db=db, prompt_identity=prompt, set_tmux_env=False,
    )
    assert prompted == ["called"]
    contents = (isolated_root / "newdev" / ".gitconfig").read_text()
    assert "Prompt Name" in contents


def test_login_dev_doesnt_overwrite_existing_gitconfig(isolated_root, stub_registry, fake_db):
    profile = isolated_root / "alice"
    profile.mkdir()
    (profile / ".gitconfig").write_text("[user]\n\tname = Existing\n\temail = e@x.com\n")
    dev_login.login_dev("alice", ["stub"], db=fake_db, set_tmux_env=False)
    contents = (profile / ".gitconfig").read_text()
    assert "Existing" in contents


def test_login_dev_unknown_cli_yields_failure_outcome(isolated_root, stub_registry, fake_db):
    outcomes = dev_login.login_dev("alice", ["nonexistent"], db=fake_db, set_tmux_env=False)
    assert len(outcomes) == 1
    assert outcomes[0].success is False
    assert "unknown" in outcomes[0].error.lower()


def test_login_dev_failure_propagates(isolated_root, stub_registry, fake_db, monkeypatch):
    monkeypatch.setattr(_StubAdapter, "login_should_succeed", False)
    outcomes = dev_login.login_dev("alice", ["stub"], db=fake_db, set_tmux_env=False)
    assert outcomes[0].success is False
    assert outcomes[0].error == "stub failure"
    assert outcomes[0].hint == "retry"


def test_login_dev_validates_id(isolated_root, stub_registry, fake_db):
    with pytest.raises(ValueError):
        dev_login.login_dev("../etc/passwd", ["stub"], db=fake_db, set_tmux_env=False)


def test_login_dev_runs_tmux_setenv_when_in_tmux(isolated_root, stub_registry, fake_db, monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,12345,0")
    with patch("dev_login.subprocess.run") as mock_run:
        dev_login.login_dev("alice", ["stub"], db=fake_db, set_tmux_env=True)
    args = mock_run.call_args[0][0]
    assert args == ["tmux", "setenv", "DEVBRAIN_DEV_ID", "alice"]


def test_login_dev_skips_tmux_setenv_outside_tmux(isolated_root, stub_registry, fake_db, monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    with patch("dev_login.subprocess.run") as mock_run:
        dev_login.login_dev("alice", ["stub"], db=fake_db, set_tmux_env=True)
    mock_run.assert_not_called()


# --- list_logins ---


def test_list_logins_empty(isolated_root, stub_registry, fake_db):
    rows = dev_login.list_logins(db=fake_db)
    assert rows == []


def test_list_logins_returns_per_dev_per_cli_rows(isolated_root, stub_registry, fake_db):
    dev_login.login_dev("alice", ["stub"], db=fake_db, set_tmux_env=False)
    dev_login.login_dev("bob", ["stub"], db=fake_db, set_tmux_env=False)
    rows = dev_login.list_logins(db=fake_db)
    assert len(rows) == 2
    by_dev = {r.dev_id: r for r in rows}
    assert by_dev["alice"].logged_in is True
    assert by_dev["bob"].logged_in is True


def test_list_logins_filtered_by_dev_id(isolated_root, stub_registry, fake_db):
    dev_login.login_dev("alice", ["stub"], db=fake_db, set_tmux_env=False)
    dev_login.login_dev("bob", ["stub"], db=fake_db, set_tmux_env=False)
    rows = dev_login.list_logins(db=fake_db, dev_id="alice")
    assert len(rows) == 1
    assert rows[0].dev_id == "alice"


def test_list_logins_validates_dev_id(isolated_root, stub_registry, fake_db):
    with pytest.raises(ValueError):
        dev_login.list_logins(db=fake_db, dev_id="../etc")


# --- logout_dev ---


def test_logout_dev_removes_profile_when_no_clis(isolated_root, stub_registry, fake_db):
    dev_login.login_dev("alice", ["stub"], db=fake_db, set_tmux_env=False)
    assert (isolated_root / "alice").exists()
    dev_login.logout_dev("alice")
    assert not (isolated_root / "alice").exists()


def test_logout_dev_partial_removes_only_cli_subdirs(isolated_root, stub_registry, fake_db):
    dev_login.login_dev("alice", ["stub"], db=fake_db, set_tmux_env=False)
    profile = isolated_root / "alice"
    assert (profile / ".stub" / "auth.json").exists()
    assert (profile / ".gitconfig").exists()

    dev_login.logout_dev("alice", cli_names=["stub"])
    assert not (profile / ".stub").exists()
    assert (profile / ".gitconfig").exists()  # gitconfig preserved
    assert profile.exists()  # profile dir preserved


def test_logout_dev_validates_id(isolated_root, stub_registry, fake_db):
    with pytest.raises(ValueError):
        dev_login.logout_dev("../etc")


def test_logout_dev_idempotent_when_profile_missing(isolated_root, stub_registry, fake_db):
    dev_login.logout_dev("nonexistent")  # no raise
