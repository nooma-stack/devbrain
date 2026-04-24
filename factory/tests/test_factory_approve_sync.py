"""Tests for factory_approve's git sync step before push.

Covers the change where approve_job runs

    git fetch origin <branch>
    git merge --ff-only origin/<branch>

in the worktree cwd before `git push -u origin <branch>`, so a
worktree whose branch tip is behind origin catches up silently
instead of pushing a non-fast-forward that git rejects.

Spec: worktree sync at approval boundary only — not per-phase.
Stubs `orchestrator.subprocess.run` to avoid touching git and
`orchestrator.notify_desktop` to avoid `osascript` calls.
"""
from __future__ import annotations

import subprocess

import pytest

import orchestrator as orch_mod
from orchestrator import FactoryOrchestrator
from state_machine import FactoryDB, JobStatus
from config import DATABASE_URL

TEST_TITLE_PREFIX = "approve_sync_test_"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture(autouse=True)
def cleanup(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.factory_jobs WHERE title LIKE %s",
            (f"{TEST_TITLE_PREFIX}%",),
        )
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            cur.execute(
                "DELETE FROM devbrain.factory_cleanup_reports "
                "WHERE job_id = ANY(%s)",
                (ids,),
            )
            cur.execute(
                "DELETE FROM devbrain.factory_artifacts "
                "WHERE job_id = ANY(%s)",
                (ids,),
            )
            cur.execute(
                "UPDATE devbrain.factory_jobs SET blocked_by_job_id = NULL "
                "WHERE blocked_by_job_id = ANY(%s)",
                (ids,),
            )
            cur.execute(
                "DELETE FROM devbrain.factory_jobs WHERE id = ANY(%s)",
                (ids,),
            )
        conn.commit()


@pytest.fixture
def orch():
    return FactoryOrchestrator(DATABASE_URL)


@pytest.fixture
def silence_notify(monkeypatch):
    """Suppress notify_desktop's osascript call on success paths."""
    monkeypatch.setattr(orch_mod, "notify_desktop", lambda *a, **k: None)


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _walk_to_ready(
    db: FactoryDB, title: str, branch_name: str,
):
    """Walk a fresh job QUEUED → ... → READY_FOR_APPROVAL with branch_name
    set. Mirrors the pattern used in test_orchestrator_cleanup.py."""
    job_id = db.create_job(
        project_slug="devbrain", title=title, spec="test",
    )
    db.transition(job_id, JobStatus.PLANNING)
    db.store_artifact(job_id, "planning", "plan", "plan body")
    db.transition(
        job_id, JobStatus.IMPLEMENTING, branch_name=branch_name,
    )
    db.store_artifact(job_id, "implementing", "diff", "diff body")
    db.transition(job_id, JobStatus.REVIEWING)
    db.store_artifact(
        job_id, "reviewing", "review", "LGTM",
        findings_count=0, blocking_count=0,
    )
    db.transition(job_id, JobStatus.QA)
    db.store_artifact(
        job_id, "qa", "qa_report", "All pass",
        findings_count=0, blocking_count=0,
    )
    db.transition(job_id, JobStatus.READY_FOR_APPROVAL)
    return db.get_job(job_id)


# ─── 1. Normal case: origin ahead, sync + push all succeed ──────────────

def test_fetch_merge_push_happy_path(orch, db, monkeypatch, silence_notify):
    """Worktree is behind origin: fetch succeeds, ff-only merge succeeds,
    push succeeds → status becomes APPROVED. Assert the call sequence is
    [fetch, merge --ff-only, push] in that order."""
    branch = "feature/approve-sync-happy"
    job = _walk_to_ready(db, f"{TEST_TITLE_PREFIX}happy", branch_name=branch)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(orch_mod.subprocess, "run", fake_run)

    result = orch.approve_job(job.id)

    assert result.status == JobStatus.APPROVED
    git_calls = [c for c in calls if c and c[0] == "git"]
    assert len(git_calls) == 3, f"expected fetch+merge+push, got: {git_calls}"
    assert git_calls[0][:3] == ["git", "fetch", "origin"]
    assert git_calls[0][-1] == branch
    assert git_calls[1][:3] == ["git", "merge", "--ff-only"]
    assert git_calls[1][-1] == f"origin/{branch}"
    assert git_calls[2][:3] == ["git", "push", "-u"]
    assert branch in git_calls[2]


# ─── 2. First push: origin has no branch yet → fetch miss, proceed ──────

def test_first_push_fetch_miss_does_not_block(
    orch, db, monkeypatch, silence_notify,
):
    """Origin doesn't have the branch yet (first push): fetch returns
    non-zero, merge is NOT attempted, push proceeds → APPROVED.
    Key assertion: the job does NOT stay at READY_FOR_APPROVAL on
    fetch miss — that's the signal for a first push, not an error."""
    branch = "feature/approve-sync-first-push"
    job = _walk_to_ready(db, f"{TEST_TITLE_PREFIX}first", branch_name=branch)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["git", "fetch"]:
            return _FakeCompleted(
                returncode=128,
                stderr=b"fatal: couldn't find remote ref "
                       b"refs/heads/" + branch.encode(),
            )
        # push succeeds
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(orch_mod.subprocess, "run", fake_run)

    result = orch.approve_job(job.id)

    assert result.status == JobStatus.APPROVED, (
        f"fetch miss must not block approval; status was {result.status}"
    )
    assert "approve_sync_error" not in result.metadata
    # Merge must NOT have been called when fetch missed.
    merge_calls = [c for c in calls if c[:3] == ["git", "merge", "--ff-only"]]
    assert merge_calls == [], (
        f"ff-only merge must not run on fetch miss; got: {merge_calls}"
    )
    # Push must have been called.
    push_calls = [c for c in calls if c[:3] == ["git", "push", "-u"]]
    assert len(push_calls) == 1


# ─── 3. Divergent history: ff-only fails → revert + approve_sync_error ──

def test_divergent_history_reverts_and_records_error(orch, db, monkeypatch):
    """Fetch succeeds but ff-only merge fails (divergent history).
    Push must NOT be attempted; job stays at READY_FOR_APPROVAL;
    metadata contains approve_sync_error with the git error tail."""
    branch = "feature/approve-sync-divergent"
    job = _walk_to_ready(db, f"{TEST_TITLE_PREFIX}divergent", branch_name=branch)
    calls: list[list[str]] = []
    merge_stderr = (
        b"hint: You have divergent branches and need to specify how to "
        b"reconcile them.\n"
        b"fatal: Not possible to fast-forward, aborting.\n"
    )

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["git", "fetch"]:
            return _FakeCompleted(returncode=0)
        if cmd[:3] == ["git", "merge", "--ff-only"]:
            return _FakeCompleted(returncode=128, stderr=merge_stderr)
        # push — must NOT be reached
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(orch_mod.subprocess, "run", fake_run)

    result = orch.approve_job(job.id)

    assert result.status == JobStatus.READY_FOR_APPROVAL, (
        "ff-only failure must leave job at READY_FOR_APPROVAL"
    )
    assert "approve_sync_error" in result.metadata
    err = result.metadata["approve_sync_error"]
    assert "fast-forward" in err or "divergent" in err, (
        f"expected git error detail in metadata, got: {err!r}"
    )
    push_calls = [c for c in calls if c[:3] == ["git", "push", "-u"]]
    assert push_calls == [], (
        f"push must not run when ff-only fails; got: {push_calls}"
    )


# ─── 4. Regression: push-fails-after-sync-succeeds preserves existing path

def test_push_fail_after_sync_preserves_existing_behavior(
    orch, db, monkeypatch, silence_notify,
):
    """Regression guard: fetch + merge succeed, but push itself fails.
    The existing push-failure behavior — log a warning and still
    transition to APPROVED — must be preserved. The spec scopes this
    PR to the sync step only; push semantics must not change."""
    branch = "feature/approve-sync-push-fail"
    job = _walk_to_ready(
        db, f"{TEST_TITLE_PREFIX}push_fail_regression", branch_name=branch,
    )
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["git", "push", "-u"]:
            # Simulate a real push failure (auth prompt hang surfaced
            # as TimeoutExpired, which the existing except branch
            # catches and swallows).
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(orch_mod.subprocess, "run", fake_run)

    result = orch.approve_job(job.id)

    # Existing behavior: push failure is swallowed → APPROVED.
    assert result.status == JobStatus.APPROVED
    assert "approve_sync_error" not in result.metadata
    # All three git calls should have fired (push raised after being invoked).
    fetch_calls = [c for c in calls if c[:2] == ["git", "fetch"]]
    merge_calls = [c for c in calls if c[:3] == ["git", "merge", "--ff-only"]]
    push_calls = [c for c in calls if c[:3] == ["git", "push", "-u"]]
    assert len(fetch_calls) == 1
    assert len(merge_calls) == 1
    assert len(push_calls) == 1


# ─── 5. Merge subprocess exception: must not silently fall through to push
#
# Addresses the PR #33 arch-review WARNING: when `subprocess.run` raises
# (TimeoutExpired, OSError) on the merge call, fetch already confirmed
# origin is ahead — pushing anyway would silently advance to stale tips
# and the existing push-swallow would mark the job APPROVED with no
# diagnostic. The fix funnels the exception into the same bail-out path
# as a non-zero returncode.

def test_merge_subprocess_exception_reverts_and_records_error(
    orch, db, monkeypatch,
):
    """Regression for the merge-exception silent-fallthrough bug.
    If `git merge --ff-only` raises after fetch confirmed origin is
    ahead, the job must stay at READY_FOR_APPROVAL with
    approve_sync_error set and push must NOT run."""
    branch = "feature/approve-sync-merge-exception"
    job = _walk_to_ready(
        db, f"{TEST_TITLE_PREFIX}merge_exception", branch_name=branch,
    )
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:2] == ["git", "fetch"]:
            return _FakeCompleted(returncode=0)
        if cmd[:3] == ["git", "merge", "--ff-only"]:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)
        # push — must NOT be reached
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(orch_mod.subprocess, "run", fake_run)

    result = orch.approve_job(job.id)

    assert result.status == JobStatus.READY_FOR_APPROVAL, (
        "merge subprocess exception must leave job at READY_FOR_APPROVAL"
    )
    assert "approve_sync_error" in result.metadata
    assert "merge subprocess failed" in result.metadata["approve_sync_error"]
    push_calls = [c for c in calls if c[:3] == ["git", "push", "-u"]]
    assert push_calls == [], (
        f"push must not run when merge subprocess raised; got: {push_calls}"
    )
