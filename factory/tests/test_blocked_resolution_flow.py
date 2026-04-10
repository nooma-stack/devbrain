"""End-to-end tests for blocked → investigate → resolve flow."""
import pytest
from unittest.mock import patch
from state_machine import FactoryDB, JobStatus
from cleanup_agent import CleanupAgent
from file_registry import FileRegistry
from orchestrator import FactoryOrchestrator

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture(autouse=True)
def cleanup(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM devbrain.file_locks WHERE dev_id LIKE 'test_bflow_%'")
        cur.execute("DELETE FROM devbrain.file_locks WHERE file_path LIKE 'src/bflow_%'")
        cur.execute("SELECT id FROM devbrain.factory_jobs WHERE title LIKE '%bflow_%'")
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            cur.execute("DELETE FROM devbrain.factory_cleanup_reports WHERE job_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM devbrain.factory_artifacts WHERE job_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM devbrain.factory_jobs WHERE id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM devbrain.devs WHERE dev_id LIKE 'test_bflow_%'")
        cur.execute("DELETE FROM devbrain.notifications WHERE recipient_dev_id LIKE 'test_bflow_%'")
        conn.commit()


def _set_submitted_by(db, job_id, dev_id):
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.factory_jobs SET submitted_by = %s WHERE id = %s",
            (dev_id, job_id),
        )
        conn.commit()


# ─── Test 1: investigate_block creates report in DB ─────────────────────

def test_blocked_flow_investigation_creates_report(db):
    """When investigate_block runs, a findings report is stored."""
    blocker_id = db.create_job(project_slug="devbrain", title="bflow_blocker_inv", spec="Test")
    registry = FileRegistry(db)
    registry.acquire_locks(
        blocker_id, db.get_job(blocker_id).project_id,
        ["src/bflow_shared_inv.py"], dev_id="alice",
    )

    blocked_id = db.create_job(project_slug="devbrain", title="bflow_blocked_inv", spec="Test")
    db.transition(blocked_id, JobStatus.PLANNING)
    db.store_artifact(blocked_id, "planning", "plan_doc", "Modify src/bflow_shared_inv.py")
    db.transition(blocked_id, JobStatus.BLOCKED)

    agent = CleanupAgent(db)
    agent.investigate_block(
        db.get_job(blocked_id),
        [{"file_path": "src/bflow_shared_inv.py", "blocking_job_id": blocker_id}],
    )

    reports = db.get_cleanup_reports(blocked_id)
    block_reports = [r for r in reports if r["report_type"] == "blocked_investigation"]
    assert len(block_reports) >= 1
    assert "bflow_blocker_inv" in block_reports[0]["summary"]


# ─── Test 2: cancel resolution → REJECTED ────────────────────────────────

def test_resolve_cancel_transitions_to_rejected(db):
    """Setting resolution=cancel and running _run_blocked transitions to REJECTED."""
    db.register_dev(dev_id="test_bflow_carol", channels=[])

    job_id = db.create_job(project_slug="devbrain", title="bflow_cancel_test", spec="Test")
    _set_submitted_by(db, job_id, "test_bflow_carol")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.BLOCKED)
    db.set_blocked_resolution(job_id, "cancel")

    orch = FactoryOrchestrator(DATABASE_URL)
    job = db.get_job(job_id)
    result = orch._run_blocked(job)

    assert result.status == JobStatus.REJECTED
    # Resolution should be cleared (consumed)
    reloaded = db.get_job(job_id)
    assert reloaded.blocked_resolution is None


# ─── Test 3: replan resolution → PLANNING ────────────────────────────────

def test_resolve_replan_transitions_to_planning(db):
    db.register_dev(dev_id="test_bflow_dave", channels=[])

    job_id = db.create_job(project_slug="devbrain", title="bflow_replan_test", spec="Test")
    _set_submitted_by(db, job_id, "test_bflow_dave")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.BLOCKED)
    db.set_blocked_resolution(job_id, "replan")

    orch = FactoryOrchestrator(DATABASE_URL)
    job = db.get_job(job_id)
    result = orch._run_blocked(job)

    assert result.status == JobStatus.PLANNING
    reloaded = db.get_job(job_id)
    assert reloaded.blocked_resolution is None


# ─── Test 4: proceed resolution with free locks → IMPLEMENTING ────────────

def test_resolve_proceed_with_free_locks(db):
    """proceed with no conflicting locks transitions to IMPLEMENTING."""
    db.register_dev(dev_id="test_bflow_eve", channels=[])

    job_id = db.create_job(project_slug="devbrain", title="bflow_proceed_test", spec="Test")
    _set_submitted_by(db, job_id, "test_bflow_eve")
    db.transition(job_id, JobStatus.PLANNING)
    db.store_artifact(job_id, "planning", "plan_doc", "Modify src/bflow_proceed_file.py")
    db.transition(job_id, JobStatus.BLOCKED)
    db.set_blocked_resolution(job_id, "proceed")

    orch = FactoryOrchestrator(DATABASE_URL)
    job = db.get_job(job_id)

    # Mock git subprocess so we don't actually create a branch on disk
    with patch("subprocess.run") as mock_sub:
        mock_sub.return_value = type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})()
        result = orch._run_blocked(job)

    assert result.status == JobStatus.IMPLEMENTING


# ─── Test 5: BLOCKED with no resolution stays BLOCKED ────────────────────

def test_blocked_without_resolution_stays_blocked(db):
    job_id = db.create_job(project_slug="devbrain", title="bflow_no_res_test", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.BLOCKED)

    orch = FactoryOrchestrator(DATABASE_URL)
    job = db.get_job(job_id)
    result = orch._run_blocked(job)

    assert result.status == JobStatus.BLOCKED


# ─── Test 6: Block notification body contains investigation summary ──────

def test_blocked_notification_includes_investigation(db):
    """Block notifications contain the investigation report summary."""
    db.register_dev(
        dev_id="test_bflow_frank",
        channels=[{"type": "tmux", "address": "test_bflow_frank"}],
    )

    blocker_id = db.create_job(project_slug="devbrain", title="bflow_notif_blocker", spec="Test")
    registry = FileRegistry(db)
    registry.acquire_locks(
        blocker_id, db.get_job(blocker_id).project_id,
        ["src/bflow_notif_shared.py"], dev_id="test_bflow_frank",
    )

    blocked_id = db.create_job(project_slug="devbrain", title="bflow_notif_blocked", spec="Test")
    _set_submitted_by(db, blocked_id, "test_bflow_frank")
    db.transition(blocked_id, JobStatus.PLANNING)
    db.store_artifact(blocked_id, "planning", "plan_doc", "Modify src/bflow_notif_shared.py")
    db.transition(blocked_id, JobStatus.BLOCKED)

    # Run investigation
    agent = CleanupAgent(db)
    report = agent.investigate_block(
        db.get_job(blocked_id),
        [{"file_path": "src/bflow_notif_shared.py", "blocking_job_id": blocker_id}],
    )

    # Fire notification with the report summary as body
    from notifications.router import NotificationRouter, NotificationEvent
    router = NotificationRouter(db)
    with patch("notifications.channels.tmux.TmuxChannel._is_session_active", return_value=False):
        router.send(NotificationEvent(
            event_type="blocked",
            recipient_dev_id="test_bflow_frank",
            title="🔒 Job blocked: bflow_notif_blocked",
            body=report.summary,
            job_id=blocked_id,
        ))

    notifs = db.get_notifications(recipient_dev_id="test_bflow_frank", limit=5)
    block_notifs = [n for n in notifs if n["event_type"] == "blocked"]
    assert len(block_notifs) >= 1
    # Body should contain the conflict file path
    assert any("bflow_notif_shared.py" in n["body"] for n in block_notifs)
    # Body should contain the recommendation
    assert any("Recommendation" in n["body"] for n in block_notifs)
