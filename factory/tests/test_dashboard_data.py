"""Tests for dashboard data queries."""
import pytest
from state_machine import FactoryDB, JobStatus
from file_registry import FileRegistry
from dashboard.data import DashboardData

from config import DATABASE_URL


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture(autouse=True)
def cleanup(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM devbrain.file_locks WHERE file_path LIKE 'src/dashtest_%'")
        cur.execute("SELECT id FROM devbrain.factory_jobs WHERE title LIKE '%dashtest_%'")
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            cur.execute("DELETE FROM devbrain.factory_cleanup_reports WHERE job_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM devbrain.factory_artifacts WHERE job_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM devbrain.factory_jobs WHERE id = ANY(%s)", (ids,))
        conn.commit()


@pytest.fixture
def data(db):
    return DashboardData(db)


def test_get_active_jobs_returns_only_active(db, data):
    """Active jobs exclude terminal states and archived."""
    active_id = db.create_job(project_slug="devbrain", title="dashtest_active", spec="Test")
    db.transition(active_id, JobStatus.PLANNING)

    failed_id = db.create_job(project_slug="devbrain", title="dashtest_failed", spec="Test")
    db.transition(failed_id, JobStatus.PLANNING)
    db.transition(failed_id, JobStatus.FAILED)

    active_jobs = data.get_active_jobs()
    titles = [j["title"] for j in active_jobs]
    assert "dashtest_active" in titles
    assert "dashtest_failed" not in titles


def test_get_active_jobs_excludes_archived(db, data):
    archived_id = db.create_job(project_slug="devbrain", title="dashtest_archived", spec="Test")
    db.transition(archived_id, JobStatus.PLANNING)
    db.transition(archived_id, JobStatus.FAILED)
    db.archive_job(archived_id)

    active = data.get_active_jobs()
    assert not any(j["title"] == "dashtest_archived" for j in active)


def test_get_recent_events_returns_artifact_events(db, data):
    job_id = db.create_job(project_slug="devbrain", title="dashtest_events", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    db.store_artifact(job_id, "planning", "plan_doc", "Test plan content")
    db.store_artifact(job_id, "reviewing", "arch_review", "Review content", blocking_count=2)

    events = data.get_recent_events(limit=20)
    titles = [e["summary"] for e in events]
    assert any("plan_doc" in s or "arch_review" in s for s in titles)


def test_get_active_file_locks(db, data):
    job_id = db.create_job(project_slug="devbrain", title="dashtest_locks", spec="Test")
    registry = FileRegistry(db)
    registry.acquire_locks(
        job_id,
        db.get_job(job_id).project_id,
        ["src/dashtest_a.py", "src/dashtest_b.py"],
        dev_id="alice",
    )

    locks = data.get_active_locks()
    paths = [l["file_path"] for l in locks]
    assert "src/dashtest_a.py" in paths
    assert "src/dashtest_b.py" in paths


def test_get_recent_completed_jobs(db, data):
    job_id = db.create_job(project_slug="devbrain", title="dashtest_completed", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.IMPLEMENTING)
    db.transition(job_id, JobStatus.REVIEWING)
    db.transition(job_id, JobStatus.QA)
    db.transition(job_id, JobStatus.READY_FOR_APPROVAL)
    db.transition(job_id, JobStatus.APPROVED)

    completed = data.get_recent_completed()
    assert any(j["title"] == "dashtest_completed" for j in completed)


def test_get_job_details(db, data):
    """get_job_details returns full job info for the detail modal."""
    job_id = db.create_job(project_slug="devbrain", title="dashtest_details", spec="Test spec")
    db.transition(job_id, JobStatus.PLANNING)
    db.store_artifact(job_id, "planning", "plan_doc", "Detailed plan")

    details = data.get_job_details(job_id)
    assert details["title"] == "dashtest_details"
    assert details["status"] == "planning"
    assert "spec" in details
    assert len(details["artifacts"]) >= 1
