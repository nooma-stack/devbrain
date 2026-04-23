"""Tests for BLOCKED state transitions."""
import pytest
from state_machine import FactoryDB, JobStatus, TRANSITIONS

from config import DATABASE_URL


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture(autouse=True)
def cleanup(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.factory_jobs WHERE title = ANY(%s)",
            ([
                "Test BLOCKED transition",
                "Resolution field test",
                "Set/clear resolution test",
                "Invalid resolution test",
            ],),
        )
        ids = [r[0] for r in cur.fetchall()]
        if ids:
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
        conn.commit()


def test_blocked_status_exists():
    assert JobStatus.BLOCKED == "blocked"


def test_waiting_status_removed():
    """WAITING no longer exists in the enum."""
    assert not hasattr(JobStatus, "WAITING")


def test_planning_can_transition_to_blocked():
    assert JobStatus.BLOCKED in TRANSITIONS[JobStatus.PLANNING]


def test_blocked_can_proceed_to_implementing():
    assert JobStatus.IMPLEMENTING in TRANSITIONS[JobStatus.BLOCKED]


def test_blocked_can_replan():
    """BLOCKED → PLANNING is valid (replan resolution)."""
    assert JobStatus.PLANNING in TRANSITIONS[JobStatus.BLOCKED]


def test_blocked_can_be_cancelled():
    """BLOCKED → REJECTED is valid (cancel resolution)."""
    assert JobStatus.REJECTED in TRANSITIONS[JobStatus.BLOCKED]


def test_blocked_can_fail_as_safety_net():
    assert JobStatus.FAILED in TRANSITIONS[JobStatus.BLOCKED]


def test_transition_planning_to_blocked(db):
    job_id = db.create_job(project_slug="devbrain", title="Test BLOCKED transition", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    job = db.transition(job_id, JobStatus.BLOCKED)
    assert job.status == JobStatus.BLOCKED


def test_factory_job_has_blocked_resolution_field(db):
    """FactoryJob has blocked_resolution field, defaults to None."""
    job_id = db.create_job(project_slug="devbrain", title="Resolution field test", spec="Test")
    job = db.get_job(job_id)
    assert hasattr(job, "blocked_resolution")
    assert job.blocked_resolution is None


def test_set_and_clear_blocked_resolution(db):
    job_id = db.create_job(project_slug="devbrain", title="Set/clear resolution test", spec="Test")
    db.set_blocked_resolution(job_id, "proceed")
    job = db.get_job(job_id)
    assert job.blocked_resolution == "proceed"
    db.clear_blocked_resolution(job_id)
    job = db.get_job(job_id)
    assert job.blocked_resolution is None


def test_set_invalid_resolution_raises(db):
    job_id = db.create_job(project_slug="devbrain", title="Invalid resolution test", spec="Test")
    with pytest.raises(ValueError, match="Invalid resolution"):
        db.set_blocked_resolution(job_id, "bogus")
