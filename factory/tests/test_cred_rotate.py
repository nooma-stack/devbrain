"""Tests for atomic credential rotation (factory/cred_rotate.py).

Real Postgres but on a throwaway test role; LaunchAgent / launchctl
calls are mocked. Filesystem state lives entirely in tmp_path so the
real .env / config/devbrain.yaml are never touched.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

import psycopg2
import pytest
from psycopg2 import sql

import cred_rotate
from config import DATABASE_URL
from cred_rotate import (
    DependentCheck,
    RotationContext,
    precheck_baseline,
    reload_dependent,
    rotate_with_dependents,
    verify_dependent,
)

TEST_ROLE = "cred_rotate_test_user"
TEST_PW_INITIAL = "cred_rotate_test_pw_initial"


@dataclass
class _FakeCompleted:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


def _admin_conn():
    return psycopg2.connect(DATABASE_URL)


@pytest.fixture
def test_role():
    """Create a throwaway Postgres role for the rotation, drop it after."""
    with _admin_conn() as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("DROP ROLE IF EXISTS {r}").format(r=sql.Identifier(TEST_ROLE))
        )
        cur.execute(
            sql.SQL("CREATE ROLE {r} LOGIN PASSWORD {pw}").format(
                r=sql.Identifier(TEST_ROLE), pw=sql.Literal(TEST_PW_INITIAL),
            )
        )
        conn.commit()
    yield TEST_ROLE
    with _admin_conn() as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL("DROP ROLE IF EXISTS {r}").format(r=sql.Identifier(TEST_ROLE))
        )
        conn.commit()


@pytest.fixture
def ctx(tmp_path, test_role):
    """RotationContext pointing at the test role + tmp env/yaml files."""
    env_path = tmp_path / ".env"
    yaml_path = tmp_path / "devbrain.yaml"
    env_path.write_text(f"DEVBRAIN_DB_PASSWORD={TEST_PW_INITIAL}\n")
    yaml_path.write_text(
        "database:\n"
        "  host: localhost\n"
        "  port: 5433\n"
        f"  user: {TEST_ROLE}\n"
        f"  password: {TEST_PW_INITIAL}\n"
        "  database: devbrain\n"
    )
    return RotationContext(
        user=TEST_ROLE, host="localhost", port=5433, database="devbrain",
        old_password=TEST_PW_INITIAL, env_path=env_path, yaml_path=yaml_path,
    )


def _make_log(tmp_path: Path, content: str = "") -> Path:
    p = tmp_path / "ingest.err.log"
    p.write_text(content)
    return p


def _healthy_launchagent_dep(tmp_path, dep_id="ingest_daemon"):
    return {
        "id": dep_id,
        "type": "launchagent",
        "label": f"com.devbrain.{dep_id}",
        "plist": str(tmp_path / f"{dep_id}.plist"),
        "verify": "tail_log_no_auth_errors",
        "verify_log": str(_make_log(tmp_path)),
        "verify_window_seconds": 1,
    }


# 1
def test_baseline_passes_with_healthy_dependents(tmp_path):
    deps = [_healthy_launchagent_dep(tmp_path)]
    checks = precheck_baseline(deps)
    assert all(c.healthy for c in checks)
    assert checks[0].id == "ingest_daemon"


# 2
def test_baseline_fails_loud_when_dependent_unhealthy(tmp_path, ctx, monkeypatch):
    # Point verify_log at a path that doesn't exist — the verifier flags
    # this as unhealthy with a clear error (per the edge-case spec). A
    # pre-populated "authentication failed" line wouldn't work because
    # the verifier snapshots start_offset = file size first to ignore
    # entries from before the rotation window.
    missing_log = tmp_path / "definitely_not_here" / "ingest.err.log"
    deps_yaml = [{
        "id": "ingest_daemon",
        "type": "launchagent",
        "plist": str(tmp_path / "p.plist"),
        "verify": "tail_log_no_auth_errors",
        "verify_log": str(missing_log),
        "verify_window_seconds": 1,
    }]
    config = {"factory": {"cred_dependents": deps_yaml}}
    altered = []

    def trip_alter(*a, **kw):
        altered.append(a)

    monkeypatch.setattr(cred_rotate, "_alter_user_password", trip_alter)

    result = rotate_with_dependents(
        ctx, "newpw_unused", config=config, require_all_healthy=True,
    )
    assert result.get("aborted_baseline") is True
    assert any(
        c.id == "ingest_daemon" and not c.healthy for c in result["unhealthy"]
    )
    assert altered == []  # never touched the DB


# 3
def test_reload_launchagent_invokes_unload_then_load(tmp_path, monkeypatch):
    plist = tmp_path / "com.devbrain.ingest.plist"
    plist.write_text("<dummy/>")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(cred_rotate.subprocess, "run", fake_run)
    reload_dependent({"id": "x", "type": "launchagent", "plist": str(plist)})
    assert len(calls) == 2
    assert calls[0][:2] == ["launchctl", "unload"]
    assert calls[1][:2] == ["launchctl", "load"]
    assert calls[0][2] == str(plist)
    assert calls[1][2] == str(plist)


# 4
def test_verify_tail_log_detects_auth_failures(tmp_path):
    log = tmp_path / "ingest.err.log"
    log.write_text("")  # start empty so the verifier's start_offset = 0

    dep = {
        "id": "ingest_daemon", "type": "launchagent",
        "verify": "tail_log_no_auth_errors",
        "verify_log": str(log), "verify_window_seconds": 2,
    }

    # Append an auth-failure mid-window to simulate a daemon that came
    # back up but couldn't reauthenticate.
    def append_after_delay():
        time.sleep(0.5)
        with log.open("a") as f:
            f.write(
                "psycopg2.OperationalError: FATAL: "
                "authentication failed for user devbrain\n"
            )

    threading.Thread(target=append_after_delay, daemon=True).start()
    check = verify_dependent(dep)
    assert check.healthy is False
    assert "authentication failed" in (check.error or "")


# 5
def test_full_rotation_happy_path(tmp_path, ctx, monkeypatch):
    log = _make_log(tmp_path)  # empty — no auth-failure lines ever appear
    deps_yaml = [{
        "id": "ingest_daemon", "type": "launchagent",
        "plist": str(tmp_path / "p.plist"),
        "verify": "tail_log_no_auth_errors",
        "verify_log": str(log), "verify_window_seconds": 1,
    }]
    config = {"factory": {"cred_dependents": deps_yaml}}
    monkeypatch.setattr(
        cred_rotate.subprocess, "run",
        lambda *a, **kw: _FakeCompleted(returncode=0),
    )
    new_pw = "rotation_test_new_password_xyz"
    result = rotate_with_dependents(ctx, new_pw, config=config)
    assert result["rolled_back"] is False
    assert any(
        c.id == "ingest_daemon" and c.healthy for c in result["reloaded"]
    )
    # New pw works
    psycopg2.connect(ctx.url(new_pw), connect_timeout=5).close()
    # .env was rewritten
    assert f"DEVBRAIN_DB_PASSWORD={new_pw}" in ctx.env_path.read_text()
    # yaml was rewritten
    assert f"password: {new_pw}" in ctx.yaml_path.read_text()


# 6
def test_rotation_rolls_back_on_dependent_verify_failure(
    tmp_path, ctx, monkeypatch,
):
    env_before = ctx.env_path.read_bytes()
    yaml_before = ctx.yaml_path.read_bytes()

    deps_yaml = [{
        "id": "ingest_daemon", "type": "launchagent",
        "plist": str(tmp_path / "p.plist"),
        "verify": "tail_log_no_auth_errors",
        "verify_log": str(_make_log(tmp_path)),
        "verify_window_seconds": 1,
    }]
    config = {"factory": {"cred_dependents": deps_yaml}}
    monkeypatch.setattr(
        cred_rotate.subprocess, "run",
        lambda *a, **kw: _FakeCompleted(returncode=0),
    )

    # Force the verifier to report failure regardless of the log state.
    def fake_verify(dep):
        return DependentCheck(
            id=dep["id"], type=dep["type"], healthy=False,
            error="forced failure for test",
        )

    monkeypatch.setattr(cred_rotate, "verify_dependent", fake_verify)

    new_pw = "test_password_should_be_rolled_back"
    result = rotate_with_dependents(
        ctx, new_pw, config=config, require_all_healthy=False,
    )
    assert result["rolled_back"] is True
    assert "ingest_daemon" in result["reason"]
    # Files reverted byte-for-byte
    assert ctx.env_path.read_bytes() == env_before
    assert ctx.yaml_path.read_bytes() == yaml_before
    # OLD password works (rollback restored it); NEW does not.
    psycopg2.connect(ctx.url(TEST_PW_INITIAL), connect_timeout=5).close()
    with pytest.raises(psycopg2.Error):
        psycopg2.connect(ctx.url(new_pw), connect_timeout=5)


# 7
def test_manual_restart_type_does_not_block_rotation(
    tmp_path, ctx, monkeypatch,
):
    deps_yaml = [
        {
            "id": "claude_desktop_mcp",
            "type": "manual_restart",
        },
        {
            "id": "ingest_daemon", "type": "launchagent",
            "plist": str(tmp_path / "p.plist"),
            "verify": "tail_log_no_auth_errors",
            "verify_log": str(_make_log(tmp_path)),
            "verify_window_seconds": 1,
        },
    ]
    config = {"factory": {"cred_dependents": deps_yaml}}
    monkeypatch.setattr(
        cred_rotate.subprocess, "run",
        lambda *a, **kw: _FakeCompleted(returncode=0),
    )

    result = rotate_with_dependents(
        ctx, "another_test_password_abc", config=config,
    )
    assert result["rolled_back"] is False
    manual_ids = [c.id for c in result["manual"]]
    assert "claude_desktop_mcp" in manual_ids
    reloaded_ids = [c.id for c in result["reloaded"]]
    assert "ingest_daemon" in reloaded_ids
