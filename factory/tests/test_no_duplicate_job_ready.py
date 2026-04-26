"""Regression tests for the duplicate `job_ready` notification.

Pre-fix the pipeline emitted two `(job_id, event_type='job_ready')` rows
about 176ms apart for every successful job: once from
`orchestrator._run_qa` on the QA → READY_FOR_APPROVAL transition, and a
second time from `cleanup_agent.run_post_cleanup` because its
`_event_type_for_status` mapping translated READY_FOR_APPROVAL to
`job_ready`. The fix removes that mapping (cleanup_agent owns FAILED
only) and hardens `_run_qa` with a terminal-status early-return so a
direct re-entry cannot re-fire the orchestrator-side emit either.

These tests lock in the contract:
  1. `_run_qa` returns immediately for terminal statuses, no notification.
  2. End-to-end run produces exactly one `job_ready` row in
     `devbrain.notifications`.
  3. The pipeline loop only calls `_run_qa` once per job (no
     accidental re-entry that would have masked test 1).
"""
import pytest

import orchestrator as orch_mod
import cleanup_agent as cleanup_mod
from orchestrator import FactoryOrchestrator
from notifications import router as router_module
from state_machine import FactoryDB, JobStatus
from config import DATABASE_URL


TEST_TITLE_PREFIX = "no_dup_test_"


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _NoopReadiness:
    """Stub for FactoryReadiness so post-cleanup doesn't shell out to git."""

    def __init__(self, *args, **kwargs):
        pass

    def ensure_ready(self):
        return []


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
                "DELETE FROM devbrain.notifications "
                "WHERE job_id = ANY(%s)", (ids,),
            )
            cur.execute(
                "DELETE FROM devbrain.factory_cleanup_reports "
                "WHERE job_id = ANY(%s)", (ids,),
            )
            cur.execute(
                "DELETE FROM devbrain.factory_artifacts "
                "WHERE job_id = ANY(%s)", (ids,),
            )
            cur.execute(
                "UPDATE devbrain.factory_jobs SET blocked_by_job_id = NULL "
                "WHERE blocked_by_job_id = ANY(%s)", (ids,),
            )
            cur.execute(
                "DELETE FROM devbrain.factory_jobs WHERE id = ANY(%s)", (ids,),
            )
        # Also clean up any test devs we registered
        cur.execute(
            "DELETE FROM devbrain.notifications "
            "WHERE recipient_dev_id LIKE 'no_dup_dev_%'"
        )
        cur.execute(
            "DELETE FROM devbrain.devs "
            "WHERE dev_id LIKE 'no_dup_dev_%'"
        )
        conn.commit()


@pytest.fixture
def orch():
    return FactoryOrchestrator(DATABASE_URL)


class _FakeRouter:
    """Captures NotificationEvent instances sent via .send()."""
    sent_events: list = []

    def __init__(self, db, *args, **kwargs):
        pass

    def send(self, event):
        type(self).sent_events.append(event)


def _stub_router(monkeypatch):
    """Patch NotificationRouter at BOTH bind sites:

      - notifications.router: orchestrator does a lazy import here on
        every emit, so it resolves the fresh attribute each call.
      - cleanup_agent: imports NotificationRouter at module load
        (`from notifications.router import NotificationRouter`), so
        the name is bound on the cleanup_agent module and patching
        notifications.router alone leaves the stale reference.
    """
    _FakeRouter.sent_events = []
    monkeypatch.setattr(router_module, "NotificationRouter", _FakeRouter)
    monkeypatch.setattr(cleanup_mod, "NotificationRouter", _FakeRouter)


def _stub_qa_subprocess(monkeypatch, *, returncode: int = 0):
    """Stub orch_mod.subprocess.run so QA's lint/test loop sees all
    checks pass. Same pattern as test_notify_job_ready.py."""
    def fake_run(cmd, **kwargs):
        return _FakeCompleted(returncode=returncode)

    monkeypatch.setattr(orch_mod.subprocess, "run", fake_run)


def _walk_to_qa(db: FactoryDB, title: str, submitted_by: str | None = None):
    """Walk a job through QUEUED → ... → QA so _run_qa sees the same
    state it would in real execution."""
    job_id = db.create_job(
        project_slug="devbrain", title=title, spec="test",
    )
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.IMPLEMENTING)
    db.transition(job_id, JobStatus.REVIEWING)
    db.store_artifact(
        job_id, "review", "arch_review", "no findings",
        blocking_count=0, warning_count=0,
    )
    db.store_artifact(
        job_id, "review", "security_review", "no findings",
        blocking_count=0, warning_count=0,
    )
    db.transition(job_id, JobStatus.QA)
    if submitted_by:
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE devbrain.factory_jobs SET submitted_by = %s WHERE id = %s",
                (submitted_by, job_id),
            )
            conn.commit()
    return db.get_job(job_id)


# ─── Test 1: re-entry guard ────────────────────────────────────────────

def test_run_qa_re_entry_on_terminal_status_emits_no_notification(
    orch, db, monkeypatch,
):
    """A direct call to _run_qa with a job already in
    READY_FOR_APPROVAL must early-return without re-firing job_ready.

    Defense against future regressions: if a caller (a fix-loop branch,
    a recovery agent, a test) ever invokes _run_qa on a converged job,
    the guard prevents a duplicate emit at the bottom of the method.
    """
    _stub_router(monkeypatch)
    _stub_qa_subprocess(monkeypatch, returncode=0)

    # Walk to QA, run once so the job converges to READY_FOR_APPROVAL.
    job = _walk_to_qa(
        db, f"{TEST_TITLE_PREFIX}reentry", submitted_by="no_dup_dev_a",
    )
    first_result = orch._run_qa(job)
    assert first_result.status == JobStatus.READY_FOR_APPROVAL
    assert len(_FakeRouter.sent_events) == 1
    assert _FakeRouter.sent_events[0].event_type == "job_ready"

    # Second call with the now-terminal job: must early-return, no emit.
    second_result = orch._run_qa(first_result)
    assert second_result.status == JobStatus.READY_FOR_APPROVAL
    assert len(_FakeRouter.sent_events) == 1, (
        f"Re-entering _run_qa on a terminal job must not fire a second "
        f"notification. Got: {[e.event_type for e in _FakeRouter.sent_events]}"
    )


# ─── Test 2: full-pipeline row count (the bug-shaped assertion) ────────

def test_full_pipeline_emits_exactly_one_job_ready_row(orch, db, monkeypatch):
    """End-to-end check using the REAL NotificationRouter so we count
    actual rows in devbrain.notifications.

    Pre-fix this returned 2 rows (orchestrator emit + cleanup_agent emit
    ~176ms later). Post-fix the cleanup_agent mapping no longer
    translates READY_FOR_APPROVAL, so only the orchestrator emit
    persists.
    """
    # Don't stub the router — we want real DB rows. Stub readiness so
    # post-cleanup doesn't shell out to git in a worktree that may not
    # exist for the test job.
    monkeypatch.setattr("readiness.FactoryReadiness", _NoopReadiness)
    _stub_qa_subprocess(monkeypatch, returncode=0)

    dev_id = "no_dup_dev_full"
    db.register_dev(
        dev_id=dev_id,
        channels=[{"type": "tmux", "address": dev_id}],
    )

    job = _walk_to_qa(
        db, f"{TEST_TITLE_PREFIX}fullpipe", submitted_by=dev_id,
    )

    # Run QA — emits orchestrator-side job_ready.
    result = orch._run_qa(job)
    assert result.status == JobStatus.READY_FOR_APPROVAL

    # Run post-cleanup as the orchestrator would for any terminal state
    # (orchestrator.py:517). Pre-fix this added a second job_ready row.
    cleanup_agent = cleanup_mod.CleanupAgent(db)
    cleanup_agent.run_post_cleanup(job.id)

    # Count rows in the real notifications table.
    rows = db.get_notifications(
        recipient_dev_id=dev_id, event_type="job_ready", limit=10,
    )
    matching = [r for r in rows if r["job_id"] == job.id]
    assert len(matching) == 1, (
        f"Expected exactly one job_ready notification row for job "
        f"{job.id}, got {len(matching)}: {matching}"
    )


# ─── Test 3: pipeline loop calls _run_qa exactly once ──────────────────

def test_pipeline_loop_invokes_run_qa_only_once(orch, db, monkeypatch):
    """The run_job loop terminates as soon as _run_qa returns
    READY_FOR_APPROVAL (it's in the loop's terminal set). Verifies the
    loop has no path that re-enters _run_qa, which is what would have
    been required for the spec's original "called twice" hypothesis to
    be true.
    """
    _stub_router(monkeypatch)
    _stub_qa_subprocess(monkeypatch, returncode=0)

    # Stub readiness for both pre- and post-job checks.
    monkeypatch.setattr("readiness.FactoryReadiness", _NoopReadiness)

    # Pre-position the job at QA so run_job only has _run_qa to do.
    # Skip planning/implementing/reviewing CLI calls.
    job = _walk_to_qa(
        db, f"{TEST_TITLE_PREFIX}loopcount", submitted_by="no_dup_dev_loop",
    )

    # Wrap _run_qa to count invocations without changing its behavior.
    call_count = {"n": 0}
    real_run_qa = orch._run_qa

    def counting_run_qa(j):
        call_count["n"] += 1
        return real_run_qa(j)

    monkeypatch.setattr(orch, "_run_qa", counting_run_qa)

    final = orch.run_job(job.id)

    assert final.status == JobStatus.READY_FOR_APPROVAL
    assert call_count["n"] == 1, (
        f"_run_qa was invoked {call_count['n']} times; the loop must "
        "exit after the first call returns READY_FOR_APPROVAL."
    )
    # And only one job_ready emit fired, regardless of how many times
    # the loop spun.
    job_ready_events = [
        e for e in _FakeRouter.sent_events if e.event_type == "job_ready"
    ]
    assert len(job_ready_events) == 1, (
        f"Expected exactly one job_ready event, got "
        f"{[e.event_type for e in _FakeRouter.sent_events]}"
    )
