"""Tests for the job_ready notification fired on qa → ready_for_approval.

Symmetric counterpart to the job_started emit at the top of _run_planning.
Pre-2026-04-25 the orchestrator never published this event, so a dev
subscribed to job_ready never heard about converged jobs — the bug
caught by the smoke-test factory job 639863cf.

Stubs NotificationRouter at its source module (matches the pattern
in test_oscillation_guardrail.py) and stubs subprocess.run to control
QA check exit codes deterministically regardless of the project's
configured lint/test commands.
"""
import pytest

import orchestrator as orch_mod
from orchestrator import FactoryOrchestrator
from state_machine import FactoryDB, JobStatus
from config import DATABASE_URL


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

TEST_TITLE_PREFIX = "notify_job_ready_test_"


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


class _FakeRouter:
    """Captures NotificationEvent instances sent via .send()."""
    sent_events: list = []

    def __init__(self, db, *args, **kwargs):
        pass

    def send(self, event):
        type(self).sent_events.append(event)


def _stub_notification_router(monkeypatch):
    """Patch NotificationRouter at its source module."""
    from notifications import router as router_module

    _FakeRouter.sent_events = []
    monkeypatch.setattr(router_module, "NotificationRouter", _FakeRouter)


def _walk_to_qa(db: FactoryDB, title: str, submitted_by: str | None = None):
    """Walk a job through QUEUED → ... → QA so _run_qa picks up where
    it would in real pipeline execution. Mirrors the helper pattern in
    test_factory_approve_sync.py."""
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


def _stub_qa_subprocess(monkeypatch, *, returncode: int = 0):
    """Stub orch_mod.subprocess.run so QA's lint/test loop sees all
    checks pass (returncode=0) or fail (returncode!=0). Uses the same
    pattern as test_orchestrator_branch_setup.py."""
    def fake_run(cmd, **kwargs):
        return _FakeCompleted(returncode=returncode, stdout="", stderr="")

    monkeypatch.setattr(orch_mod.subprocess, "run", fake_run)


def test_job_ready_notification_fires_on_qa_pass(orch, db, monkeypatch):
    """Happy path: QA returns all_passed=True → transition to
    READY_FOR_APPROVAL → notification with event_type='job_ready' fires
    once, carrying the job id and title."""
    _stub_notification_router(monkeypatch)
    _stub_qa_subprocess(monkeypatch, returncode=0)

    job = _walk_to_qa(
        db, f"{TEST_TITLE_PREFIX}happy", submitted_by="test-dev-ready",
    )

    result = orch._run_qa(job)

    assert result.status == JobStatus.READY_FOR_APPROVAL
    assert len(_FakeRouter.sent_events) == 1
    event = _FakeRouter.sent_events[0]
    assert event.event_type == "job_ready"
    assert event.recipient_dev_id == "test-dev-ready"
    assert event.job_id == job.id
    assert "ready for approval" in event.title.lower()


def test_job_ready_notification_skips_when_no_submitted_by(orch, db, monkeypatch):
    """If job.submitted_by is None there is nobody to notify — the
    early-return branch fires before any NotificationEvent is built.
    The READY_FOR_APPROVAL transition itself still commits."""
    _stub_notification_router(monkeypatch)
    _stub_qa_subprocess(monkeypatch, returncode=0)

    job = _walk_to_qa(db, f"{TEST_TITLE_PREFIX}no_submitter")
    assert job.submitted_by is None

    result = orch._run_qa(job)

    assert result.status == JobStatus.READY_FOR_APPROVAL
    assert _FakeRouter.sent_events == []


def test_job_ready_not_fired_when_qa_fails(orch, db, monkeypatch):
    """QA failure routes back to FIX_LOOP — no job_ready notification
    fires (the job isn't ready). Locks in that the emit is gated on the
    happy-path branch only."""
    _stub_notification_router(monkeypatch)
    _stub_qa_subprocess(monkeypatch, returncode=1)

    job = _walk_to_qa(
        db, f"{TEST_TITLE_PREFIX}qa_fail", submitted_by="test-dev-fail",
    )

    result = orch._run_qa(job)

    assert result.status == JobStatus.FIX_LOOP
    assert _FakeRouter.sent_events == []
