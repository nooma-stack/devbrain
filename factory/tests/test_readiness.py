"""Tests for factory readiness: fetch-origin + behind_origin detection.

These tests cover the auto-pull behavior added so the factory-host
checkout is synced to origin/<base_branch> before every job runs. The
motivating incident: factory job a51efc39 (PR #34) ran with
orchestrator.py at commit d2593c9 because no one had pulled; the
updated parser it was testing was never actually executed.

Pattern: stub `readiness.subprocess.run` via monkeypatch with a
_FakeCompleted helper, and mock _check_orphan_locks to return [] so
the dev DB's file_locks state can't pollute assertions. No real git.
Autouse cleanup handles any factory_runtime_state rows the flag-
persistence path writes.
"""
from __future__ import annotations

import logging
import subprocess

import pytest

import readiness as readiness_module
from readiness import FactoryReadiness, ReadinessIssue
from state_machine import FactoryDB
from config import DATABASE_URL

TEST_PROJECT_ROOT = "/tmp/readiness_test_root"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture(autouse=True)
def cleanup(db):
    """Clear the not_ready flag row before and after each test so flag
    persistence from one test can't leak into the next."""
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM devbrain.factory_runtime_state WHERE key = 'not_ready'"
        )
        conn.commit()


@pytest.fixture
def readiness(db, monkeypatch):
    """FactoryReadiness with _check_orphan_locks stubbed to []. Tests
    that need a custom base_branch build their own instance."""
    r = FactoryReadiness(db, TEST_PROJECT_ROOT)
    monkeypatch.setattr(r, "_check_orphan_locks", lambda: [])
    return r


class _FakeCompleted:
    """Mirror the _FakeCompleted helper in test_factory_approve_sync.py.
    stdout defaults to bytes because _fetch_origin calls subprocess.run
    WITHOUT text=True, while _run_git uses text=True. Tests decide which
    form to return based on the command dispatched in the stub.
    """
    def __init__(self, returncode: int = 0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _default_git_response(cmd):
    """Return a happy-path _FakeCompleted for the given git command.
    Bytes for fetch (no text=True), strings for _run_git (text=True).
    """
    if cmd[:2] == ["git", "fetch"]:
        return _FakeCompleted(returncode=0, stdout=b"", stderr=b"")
    if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
        return _FakeCompleted(returncode=0, stdout="main\n", stderr="")
    if cmd[:2] == ["git", "status"]:
        return _FakeCompleted(returncode=0, stdout="", stderr="")
    if cmd[:3] == ["git", "rev-list", "--count"]:
        return _FakeCompleted(returncode=0, stdout="0\n", stderr="")
    return _FakeCompleted(returncode=0, stdout="", stderr="")


# ─── 1. Fetch runs first ────────────────────────────────────────────────

def test_fetch_is_first_subprocess_call(readiness, monkeypatch):
    """`git fetch origin main` must be the very first subprocess call
    ensure_ready() issues — before any verify step. This guarantees
    the behind_origin check that follows compares against fresh refs.
    """
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _default_git_response(cmd)

    monkeypatch.setattr(readiness_module.subprocess, "run", fake_run)

    remaining = readiness.ensure_ready()

    assert remaining == []
    assert len(calls) >= 1
    assert calls[0][:4] == ["git", "fetch", "origin", "main"]


# ─── 2. Fetch timeout is best-effort ────────────────────────────────────

def test_fetch_timeout_does_not_raise_or_emit_issue(
    readiness, monkeypatch, caplog,
):
    """Offline factory-host: fetch raises TimeoutExpired. ensure_ready()
    must NOT raise, must NOT emit a readiness issue for the fetch
    failure, and must log a WARNING. Local code still runs.
    """
    caplog.set_level(logging.WARNING, logger="readiness")

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["git", "fetch"]:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)
        return _default_git_response(cmd)

    monkeypatch.setattr(readiness_module.subprocess, "run", fake_run)

    remaining = readiness.ensure_ready()

    assert remaining == []
    assert any(
        "fetch" in rec.message.lower() for rec in caplog.records
    ), f"expected a fetch-related WARNING, got: {[r.message for r in caplog.records]}"


# ─── 3. behind_origin emitted when HEAD is behind ───────────────────────

def test_behind_origin_issue_emitted_when_count_positive(
    readiness, monkeypatch,
):
    """rev-list --count returns a positive number → behind_origin issue
    with commits_behind detail and auto_repairable=True.
    """
    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "rev-list", "--count"]:
            return _FakeCompleted(returncode=0, stdout="3\n", stderr="")
        return _default_git_response(cmd)

    monkeypatch.setattr(readiness_module.subprocess, "run", fake_run)

    issues = readiness.verify()
    kinds = [i.kind for i in issues]

    assert "behind_origin" in kinds, f"expected behind_origin; got: {kinds}"
    bo = next(i for i in issues if i.kind == "behind_origin")
    assert bo.details == {"commits_behind": 3}
    assert bo.auto_repairable is True


# ─── 4. Up-to-date emits no behind_origin ───────────────────────────────

def test_up_to_date_emits_no_behind_origin_issue(readiness, monkeypatch):
    """rev-list --count returns 0 → no behind_origin issue."""
    def fake_run(cmd, **kwargs):
        return _default_git_response(cmd)

    monkeypatch.setattr(readiness_module.subprocess, "run", fake_run)

    issues = readiness.verify()

    assert all(i.kind != "behind_origin" for i in issues), (
        f"expected no behind_origin issue; got: {[i.kind for i in issues]}"
    )


# ─── 5. behind_origin repair runs reset + clean ─────────────────────────

def test_behind_origin_repair_runs_reset_and_clean(readiness, monkeypatch):
    """attempt_repair for a behind_origin issue must run
    `git reset --hard origin/main` + `git clean -fd` (shared primitive
    with dirty_working_tree).
    """
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _default_git_response(cmd)

    monkeypatch.setattr(readiness_module.subprocess, "run", fake_run)

    issue = ReadinessIssue(
        kind="behind_origin",
        message="HEAD is 5 commit(s) behind origin/main",
        details={"commits_behind": 5},
    )
    readiness.attempt_repair([issue])

    reset_calls = [c for c in calls if c[:3] == ["git", "reset", "--hard"]]
    clean_calls = [c for c in calls if c[:3] == ["git", "clean", "-fd"]]
    assert len(reset_calls) == 1, f"expected one reset; got: {reset_calls}"
    assert reset_calls[0][-1] == "origin/main"
    assert len(clean_calls) == 1, f"expected one clean; got: {clean_calls}"


# ─── 6. Configured base_branch flows through ────────────────────────────

def test_fetch_uses_configured_base_branch(db, monkeypatch):
    """A FactoryReadiness built with base_branch="develop" must fetch
    origin/develop and compare HEAD..origin/develop — not main.
    """
    r = FactoryReadiness(db, TEST_PROJECT_ROOT, base_branch="develop")
    monkeypatch.setattr(r, "_check_orphan_locks", lambda: [])

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return _FakeCompleted(returncode=0, stdout="develop\n", stderr="")
        return _default_git_response(cmd)

    monkeypatch.setattr(readiness_module.subprocess, "run", fake_run)

    r.ensure_ready()

    fetch_calls = [c for c in calls if c[:2] == ["git", "fetch"]]
    revlist_calls = [c for c in calls if c[:3] == ["git", "rev-list", "--count"]]
    assert fetch_calls[0] == ["git", "fetch", "origin", "develop"]
    assert revlist_calls[0][-1] == "HEAD..origin/develop"
