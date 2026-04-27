"""Tests for setup.setup_multi_dev — wire a remote/shared Postgres URL.

The plan's gate is: connection test BEFORE .env is touched. These tests
mock psycopg2.connect so they never need a real DB, and redirect
DEVBRAIN_HOME at the setup module so .env writes land in a tmp_path.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

import setup
from cli import cli


# ─── Helpers ──────────────────────────────────────────────────────────────

class _FakeCursor:
    def __init__(self, version: str = "PostgreSQL 17.0 on x86_64-linux-gnu"):
        self._version = version

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql):  # noqa: ARG002 — interface match
        return None

    def fetchone(self):
        return (self._version,)


class _FakeConn:
    def __init__(self, version: str = "PostgreSQL 17.0 on x86_64-linux-gnu"):
        self._version = version
        self.closed = False

    def cursor(self):
        return _FakeCursor(self._version)

    def close(self):
        self.closed = True


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """Redirect setup.DEVBRAIN_HOME so .env writes go to a tmp dir."""
    monkeypatch.setattr(setup, "DEVBRAIN_HOME", tmp_path)
    return tmp_path


# ─── Tests ────────────────────────────────────────────────────────────────

def test_setup_multi_dev_writes_env_after_successful_connection(
    isolated_env, monkeypatch
):
    """Happy path: connection succeeds, DEVBRAIN_DATABASE_URL lands in .env."""
    captured = {}

    def fake_connect(url, connect_timeout=5):
        captured["url"] = url
        captured["timeout"] = connect_timeout
        return _FakeConn()

    monkeypatch.setattr("psycopg2.connect", fake_connect)

    ok = setup.setup_multi_dev(
        host="db.example.com", port=5432, database="devbrain",
        username="alice", password="hunter2",
        non_interactive=True,
    )
    assert ok is True

    env_path = isolated_env / ".env"
    assert env_path.exists()
    body = env_path.read_text()
    assert "DEVBRAIN_DATABASE_URL=" in body
    # Connection test was made with the same URL we wrote.
    assert "postgresql://alice:hunter2@db.example.com:5432/devbrain" in body
    assert captured["url"] == "postgresql://alice:hunter2@db.example.com:5432/devbrain"
    # 0600 permissions enforced by _append_env.
    assert (env_path.stat().st_mode & 0o777) == 0o600


def test_setup_multi_dev_does_not_write_env_on_connection_failure(
    isolated_env, monkeypatch
):
    """Failure gate: if the connection test raises, .env is NOT touched."""
    def fake_connect(*args, **kwargs):
        raise RuntimeError("boom: could not connect")

    monkeypatch.setattr("psycopg2.connect", fake_connect)

    ok = setup.setup_multi_dev(
        host="bad.example.com", port=5432, database="devbrain",
        username="alice", password="hunter2",
        non_interactive=True,
    )
    assert ok is False
    assert not (isolated_env / ".env").exists()


def test_setup_multi_dev_url_encodes_special_chars(isolated_env, monkeypatch):
    """Passwords with @ : / # spaces are URL-encoded so URL parses correctly."""
    captured = {}

    def fake_connect(url, connect_timeout=5):
        captured["url"] = url
        return _FakeConn()

    monkeypatch.setattr("psycopg2.connect", fake_connect)

    ok = setup.setup_multi_dev(
        host="db.example.com", port=5432, database="devbrain",
        username="alice@corp", password="p@ss:wo/rd #1",
        non_interactive=True,
    )
    assert ok is True

    url = captured["url"]
    # Special chars survived as percent-encoded — no raw "@", ":", "/", "#"
    # in the password substring.
    assert "alice%40corp" in url
    assert "p%40ss%3Awo%2Frd+%231" in url or "p%40ss%3Awo%2Frd%20%231" in url
    # And the .env file contains the same encoded URL.
    assert url in (isolated_env / ".env").read_text()


def test_setup_multi_dev_requires_all_flags_in_non_interactive_mode(
    isolated_env, monkeypatch
):
    """Non-interactive: missing flag → return False, .env untouched."""
    def fake_connect(*args, **kwargs):
        pytest.fail("connection should not be attempted with missing flag")

    monkeypatch.setattr("psycopg2.connect", fake_connect)

    ok = setup.setup_multi_dev(
        host="db.example.com", port=5432, database="devbrain",
        username=None, password="hunter2",  # username missing
        non_interactive=True,
    )
    assert ok is False
    assert not (isolated_env / ".env").exists()


def test_setup_multi_dev_is_idempotent(isolated_env, monkeypatch):
    """Re-running upserts the same key — exactly one DEVBRAIN_DATABASE_URL line."""
    monkeypatch.setattr(
        "psycopg2.connect", lambda *a, **kw: _FakeConn()
    )

    for _ in range(2):
        assert setup.setup_multi_dev(
            host="db.example.com", port=5432, database="devbrain",
            username="alice", password="hunter2",
            non_interactive=True,
        ) is True

    body = (isolated_env / ".env").read_text()
    assert body.count("DEVBRAIN_DATABASE_URL=") == 1


def test_setup_multi_dev_cli_command_exits_nonzero_on_connection_failure(
    isolated_env, monkeypatch, runner
):
    """CLI: scripted command surfaces failure as non-zero exit code."""
    def fake_connect(*args, **kwargs):
        raise RuntimeError("password authentication failed")

    monkeypatch.setattr("psycopg2.connect", fake_connect)

    result = runner.invoke(cli, [
        "setup-multi-dev",
        "--host", "db.example.com",
        "--port", "5432",
        "--database", "devbrain",
        "--username", "alice",
        "--password", "hunter2",
    ])
    assert result.exit_code != 0
    assert not (isolated_env / ".env").exists()


def test_setup_multi_dev_cli_command_writes_env_on_success(
    isolated_env, monkeypatch, runner
):
    """CLI: scripted command writes .env on a successful connection test."""
    monkeypatch.setattr("psycopg2.connect", lambda *a, **kw: _FakeConn())

    result = runner.invoke(cli, [
        "setup-multi-dev",
        "--host", "db.example.com",
        "--port", "5432",
        "--database", "devbrain",
        "--username", "alice",
        "--password", "hunter2",
    ])
    assert result.exit_code == 0
    env_body = (isolated_env / ".env").read_text()
    assert "DEVBRAIN_DATABASE_URL=postgresql://alice:hunter2@db.example.com:5432/devbrain" in env_body
