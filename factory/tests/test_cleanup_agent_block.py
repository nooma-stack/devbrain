"""Tests for cleanup agent's block investigation."""
import pytest
from state_machine import FactoryDB, JobStatus
from cleanup_agent import CleanupAgent
from file_registry import FileRegistry

from config import DATABASE_URL


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture(autouse=True)
def cleanup(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM devbrain.file_locks WHERE dev_id LIKE 'test_block_%'")
        cur.execute("SELECT id FROM devbrain.factory_jobs WHERE title LIKE '%blockinv_%'")
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            cur.execute("DELETE FROM devbrain.factory_cleanup_reports WHERE job_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM devbrain.factory_artifacts WHERE job_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM devbrain.factory_jobs WHERE id = ANY(%s)", (ids,))
        conn.commit()


def test_investigate_block_returns_cleanup_report(db):
    """investigate_block returns a CleanupReport with report_type='blocked_investigation'."""
    blocker_id = db.create_job(project_slug="devbrain", title="blockinv_blocker_1", spec="Test")
    registry = FileRegistry(db)
    registry.acquire_locks(
        blocker_id, db.get_job(blocker_id).project_id,
        ["src/shared_block.py"], dev_id="alice",
    )

    blocked_id = db.create_job(project_slug="devbrain", title="blockinv_blocked_1", spec="Test")
    db.transition(blocked_id, JobStatus.PLANNING)
    db.store_artifact(blocked_id, "planning", "plan_doc", "Plan: modify src/shared_block.py")
    db.transition(blocked_id, JobStatus.BLOCKED)

    conflicts = [{"file_path": "src/shared_block.py", "blocking_job_id": blocker_id}]

    agent = CleanupAgent(db)
    job = db.get_job(blocked_id)
    report = agent.investigate_block(job, conflicts)

    assert report.report_type == "blocked_investigation"
    assert report.outcome == "awaiting_resolution"
    assert "src/shared_block.py" in report.summary
    assert "blockinv_blocker_1" in report.summary
    assert report.metadata.get("blocking_job_ids") == [blocker_id]
    assert report.metadata.get("recommendation") in ("proceed", "replan", "cancel", "wait")


def test_investigate_block_persists_report(db):
    """The investigation report is saved to factory_cleanup_reports."""
    blocker_id = db.create_job(project_slug="devbrain", title="blockinv_blocker_2", spec="Test")
    registry = FileRegistry(db)
    registry.acquire_locks(
        blocker_id, db.get_job(blocker_id).project_id,
        ["src/shared_block2.py"], dev_id="alice",
    )

    blocked_id = db.create_job(project_slug="devbrain", title="blockinv_blocked_2", spec="Test")
    db.transition(blocked_id, JobStatus.PLANNING)
    db.store_artifact(blocked_id, "planning", "plan_doc", "Plan: edit src/shared_block2.py")
    db.transition(blocked_id, JobStatus.BLOCKED)

    conflicts = [{"file_path": "src/shared_block2.py", "blocking_job_id": blocker_id}]
    agent = CleanupAgent(db)
    agent.investigate_block(db.get_job(blocked_id), conflicts)

    reports = db.get_cleanup_reports(blocked_id)
    assert any(r["report_type"] == "blocked_investigation" for r in reports)


def test_investigate_block_includes_blocking_dev(db):
    """The summary and metadata include info about the blocking dev."""
    blocker_id = db.create_job(
        project_slug="devbrain", title="blockinv_blocker_3", spec="Test",
    )
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.factory_jobs SET submitted_by = %s WHERE id = %s",
            ("patrick", blocker_id),
        )
        conn.commit()

    blocked_id = db.create_job(project_slug="devbrain", title="blockinv_blocked_3", spec="Test")
    db.transition(blocked_id, JobStatus.PLANNING)
    db.transition(blocked_id, JobStatus.BLOCKED)

    conflicts = [{"file_path": "src/x.py", "blocking_job_id": blocker_id}]
    agent = CleanupAgent(db)
    report = agent.investigate_block(db.get_job(blocked_id), conflicts)

    assert "patrick" in report.summary or "blockinv_blocker_3" in report.summary
    assert report.metadata.get("blocking_dev_id") == "patrick"


def test_investigate_block_with_no_conflicts(db):
    """Handles edge case of empty conflicts list gracefully."""
    job_id = db.create_job(project_slug="devbrain", title="blockinv_blocked_empty", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.BLOCKED)

    agent = CleanupAgent(db)
    report = agent.investigate_block(db.get_job(job_id), [])
    # Should still return a valid report, just with no conflict data
    assert report.report_type == "blocked_investigation"
