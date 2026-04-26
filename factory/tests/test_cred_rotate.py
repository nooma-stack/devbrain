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
    psycopg2.connect(**ctx.connect_kwargs(new_pw), connect_timeout=5).close()
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
    # Pre-flight calls with lookback=True; post-reload calls with the
    # default (lookback=False) — accept both. Use the actual verifier for
    # pre-flight (so the test's healthy log passes baseline) and force
    # failure only on the post-reload path.
    real_verify = cred_rotate.verify_dependent

    def fake_verify(dep, *, lookback=False):
        if lookback:
            return real_verify(dep, lookback=True)
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
    psycopg2.connect(
        **ctx.connect_kwargs(TEST_PW_INITIAL), connect_timeout=5,
    ).close()
    with pytest.raises(psycopg2.Error):
        psycopg2.connect(**ctx.connect_kwargs(new_pw), connect_timeout=5)


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


# 8 — finding 8.b: --skip-dependents bypasses the registry entirely.
def test_skip_dependents_bypasses_registry(tmp_path, ctx, monkeypatch):
    log = _make_log(tmp_path)
    # Even a *broken* dependent (missing log) must not block rotation
    # when skip_dependents=True.
    deps_yaml = [{
        "id": "ingest_daemon", "type": "launchagent",
        "plist": str(tmp_path / "p.plist"),
        "verify": "tail_log_no_auth_errors",
        "verify_log": str(tmp_path / "definitely_not_here.log"),
        "verify_window_seconds": 1,
    }]
    config = {"factory": {"cred_dependents": deps_yaml}}

    subprocess_calls = []

    def track_subprocess(*a, **kw):
        subprocess_calls.append(a)
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(cred_rotate.subprocess, "run", track_subprocess)

    new_pw = "skip_dep_test_pw_xyz"
    result = rotate_with_dependents(
        ctx, new_pw, config=config, skip_dependents=True,
    )
    assert result["rolled_back"] is False
    assert result["skipped"] is True
    assert result["reloaded"] == []
    # No launchctl calls — registry was bypassed completely.
    assert subprocess_calls == []
    # New password works.
    psycopg2.connect(**ctx.connect_kwargs(new_pw), connect_timeout=5).close()
    # log file untouched (was created by _make_log only as a fixture)
    assert log.exists()


# 9 — finding 8.d: require_all_healthy=False proceeds past unhealthy deps.
def test_no_require_all_healthy_proceeds_past_unhealthy(
    tmp_path, ctx, monkeypatch,
):
    # Pre-flight will see this dep as unhealthy (log doesn't exist).
    deps_yaml = [{
        "id": "broken_dep", "type": "launchagent",
        "plist": str(tmp_path / "broken.plist"),
        "verify": "tail_log_no_auth_errors",
        "verify_log": str(tmp_path / "missing.log"),
        "verify_window_seconds": 1,
    }]
    config = {"factory": {"cred_dependents": deps_yaml}}
    monkeypatch.setattr(
        cred_rotate.subprocess, "run",
        lambda *a, **kw: _FakeCompleted(returncode=0),
    )

    new_pw = "no_require_all_healthy_pw"
    result = rotate_with_dependents(
        ctx, new_pw, config=config, require_all_healthy=False,
    )
    # Pre-flight didn't abort (no aborted_baseline) but post-reload verify
    # still fails (missing log) → triggers rollback.
    assert result.get("aborted_baseline") is not True
    assert result["rolled_back"] is True
    # OLD password still works after rollback.
    psycopg2.connect(
        **ctx.connect_kwargs(TEST_PW_INITIAL), connect_timeout=5,
    ).close()


# 10 — finding 8.a: sanity-check failure at step 3 triggers full rollback.
def test_sanity_check_failure_triggers_rollback(tmp_path, ctx, monkeypatch):
    env_before = ctx.env_path.read_bytes()
    yaml_before = ctx.yaml_path.read_bytes()
    config = {"factory": {"cred_dependents": []}}

    real_connect = psycopg2.connect
    real_alter = cred_rotate._alter_user_password
    state = {"alter_count": 0, "in_alter": False}

    def wrapped_alter(c, current, new):
        state["alter_count"] += 1
        state["in_alter"] = True
        try:
            return real_alter(c, current, new)
        finally:
            state["in_alter"] = False

    def flaky_connect(*args, **kwargs):
        # Fail any direct connect that isn't coming from inside
        # _alter_user_password — that's the sanity check at step 3.
        if not state["in_alter"]:
            raise psycopg2.OperationalError("simulated sanity-check failure")
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(cred_rotate, "_alter_user_password", wrapped_alter)
    monkeypatch.setattr(cred_rotate.psycopg2, "connect", flaky_connect)

    new_pw = "sanity_check_failure_pw"
    result = rotate_with_dependents(ctx, new_pw, config=config)
    assert result["rolled_back"] is True
    assert "sanity check connect failed" in result["reason"]
    # ALTER USER was issued twice: forward then rollback.
    assert state["alter_count"] == 2
    # Files were restored byte-for-byte.
    assert ctx.env_path.read_bytes() == env_before
    assert ctx.yaml_path.read_bytes() == yaml_before
    # OLD password works after rollback (use real_connect — flaky_connect
    # is still installed via the active monkeypatch).
    real_connect(
        **ctx.connect_kwargs(TEST_PW_INITIAL), connect_timeout=5,
    ).close()


# 11 — finding 8.e + finding 5: multi-dep partial-failure rollback
# re-reloads dependents that already picked up the new (now-reverted) creds.
def test_multi_dep_partial_failure_re_reloads_on_rollback(
    tmp_path, ctx, monkeypatch,
):
    dep_a = _healthy_launchagent_dep(tmp_path, dep_id="dep_a")
    dep_b = _healthy_launchagent_dep(tmp_path, dep_id="dep_b")
    config = {"factory": {"cred_dependents": [dep_a, dep_b]}}

    monkeypatch.setattr(
        cred_rotate.subprocess, "run",
        lambda *a, **kw: _FakeCompleted(returncode=0),
    )

    # Track every reload_dependent call (forward + rollback).
    reload_calls: list[str] = []
    real_reload = cred_rotate.reload_dependent

    def tracking_reload(dep):
        reload_calls.append(dep["id"])
        return real_reload(dep)

    monkeypatch.setattr(cred_rotate, "reload_dependent", tracking_reload)

    # dep_a verifies healthy; dep_b verifies unhealthy → triggers rollback.
    real_verify = cred_rotate.verify_dependent

    def selective_verify(dep, *, lookback=False):
        if lookback:
            return real_verify(dep, lookback=True)
        if dep["id"] == "dep_b":
            return DependentCheck(
                id=dep["id"], type=dep["type"], healthy=False,
                error="forced post-reload failure for dep_b",
            )
        return real_verify(dep)

    monkeypatch.setattr(cred_rotate, "verify_dependent", selective_verify)

    new_pw = "multi_dep_partial_failure_pw"
    result = rotate_with_dependents(ctx, new_pw, config=config)
    assert result["rolled_back"] is True
    # Forward path reloaded dep_a then dep_b; rollback re-reloaded dep_a
    # (the one that already picked up the new — now reverted — creds).
    # dep_b is NOT re-reloaded because it never made it into `reloaded`.
    assert reload_calls == ["dep_a", "dep_b", "dep_a"]
    assert "reload_rollback_errors" not in result


# 12 — finding 8.f: rollback ALTER USER itself failing surfaces a
# distinct rollback_failed result instead of propagating the exception.
def test_rollback_alter_user_failure_returns_rollback_failed(
    tmp_path, ctx, monkeypatch,
):
    config = {"factory": {"cred_dependents": []}}

    real_alter = cred_rotate._alter_user_password
    real_connect = psycopg2.connect
    state = {"alter_count": 0, "in_alter": False}

    def alter_then_fail(c, current, new):
        state["alter_count"] += 1
        if state["alter_count"] == 1:
            state["in_alter"] = True
            try:
                return real_alter(c, current, new)
            finally:
                state["in_alter"] = False
        raise psycopg2.OperationalError(
            "simulated rollback ALTER USER failure (transient connectivity)"
        )

    monkeypatch.setattr(cred_rotate, "_alter_user_password", alter_then_fail)

    # Fail the sanity-check connect to force entry into the rollback
    # path; allow alter-internal connects (in_alter=True) through.
    def selective_connect(*args, **kwargs):
        if state["in_alter"]:
            return real_connect(*args, **kwargs)
        raise psycopg2.OperationalError("forced sanity-check failure")

    monkeypatch.setattr(cred_rotate.psycopg2, "connect", selective_connect)

    new_pw = "rollback_failure_test_pw"
    result = rotate_with_dependents(ctx, new_pw, config=config)
    assert result.get("rollback_failed") is True
    assert result["rolled_back"] is False
    assert "ALTER USER rollback failed" in result["rollback_error"]
    assert "sanity check connect failed" in result["reason"]
    assert state["alter_count"] == 2  # forward succeeded, rollback raised


# 13 — finding 3 + 6: connect_via_proxy verifier fails closed.
def test_connect_via_proxy_fails_closed(tmp_path):
    dep = {
        "id": "proxy_dep", "type": "launchagent",
        "verify": "connect_via_proxy",
    }
    check = verify_dependent(dep)
    assert check.healthy is False
    assert "not implemented" in (check.error or "").lower()


# 14 — finding 4: pre-flight catches pre-existing auth-failure churn
# in the log tail (the 18-day stale-creds incident this feature exists
# to prevent), even when no retry happens during the verify window.
def test_precheck_lookback_catches_existing_auth_failure_in_log(tmp_path):
    log = tmp_path / "ingest.err.log"
    # Pre-existing auth failure from BEFORE the rotation window starts.
    log.write_text(
        "2026-04-25 03:14:07 psycopg2.OperationalError: FATAL: "
        "authentication failed for user devbrain\n"
    )
    dep = {
        "id": "stale_creds_dep", "type": "launchagent",
        "plist": str(tmp_path / "p.plist"),
        "verify": "tail_log_no_auth_errors",
        "verify_log": str(log),
        # 0-second window so the test doesn't wait — lookback alone must
        # be sufficient to catch the existing entry.
        "verify_window_seconds": 0,
    }
    checks = precheck_baseline([dep])
    assert checks[0].healthy is False
    assert "authentication failed" in (checks[0].error or "")
    # Post-reload verify (lookback=False) must NOT flag pre-existing
    # entries — otherwise rotation could never succeed against a daemon
    # that had any prior auth failure.
    post = verify_dependent(dep)
    assert post.healthy is True


# 15 — finding 8.c: --current-password is a flag (no value), prompts
# securely for the password, never accepts it on the command line.
def test_current_password_flag_prompts_and_never_takes_value():
    """The CLI option must NOT accept a string value.

    Regression guard: a value-taking option leaks the password into
    `ps aux`, shell history, and process-monitoring captures. Verify the
    option's Click metadata is `is_flag=True` so passing
    `--current-password VALUE` is rejected.
    """
    from click.testing import CliRunner

    from cli import cli as devbrain_cli

    runner = CliRunner()
    # Locate the param on the command.
    rotate_cmd = devbrain_cli.commands["rotate-db-password"]
    cur_param = next(
        p for p in rotate_cmd.params
        if "--current-password" in p.opts
    )
    assert cur_param.is_flag is True, (
        "--current-password must be a flag (no value) so the password "
        "never appears in `ps aux` or shell history"
    )
    # Help text warns operators against passing the password.
    result = runner.invoke(devbrain_cli, ["rotate-db-password", "--help"])
    assert result.exit_code == 0
    assert "WARNING" in result.output
    assert "ps aux" in result.output
