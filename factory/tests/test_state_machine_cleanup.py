"""Tests for cleanup-related state machine methods."""
import json
import pytest
from state_machine import FactoryDB, JobStatus

from config import DATABASE_URL


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture
def test_job(db):
    """Create a test job in FAILED state for cleanup testing."""
    job_id = db.create_job(project_slug="devbrain", title="Test cleanup job", spec="Test spec")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.FAILED)
    return job_id


def test_archive_job(db, test_job):
    """Archiving a job sets archived_at timestamp."""
    db.archive_job(test_job)
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT archived_at FROM devbrain.factory_jobs WHERE id = %s", (test_job,))
        assert cur.fetchone()[0] is not None


def test_archive_non_terminal_job_raises(db):
    """Cannot archive a job that isn't in a terminal state."""
    job_id = db.create_job(project_slug="devbrain", title="Test active job", spec="Test spec")
    with pytest.raises(ValueError, match="Cannot archive"):
        db.archive_job(job_id)


def test_store_cleanup_report(db, test_job):
    """Store and retrieve a cleanup report."""
    report_id = db.store_cleanup_report(
        job_id=test_job, report_type="post_run", outcome="clean",
        summary="Job completed successfully.", phases_traversed=["queued", "planning", "failed"],
        time_elapsed_seconds=5,
    )
    assert report_id is not None
    reports = db.get_cleanup_reports(test_job)
    assert len(reports) >= 1
    assert reports[-1]["outcome"] == "clean"


def test_list_jobs_excludes_archived(db, test_job):
    """Archived jobs don't appear in active_only queries."""
    db.archive_job(test_job)
    active = db.list_jobs(project_slug="devbrain", active_only=True)
    assert all(j.id != test_job for j in active)
