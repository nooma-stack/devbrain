"""Tests for WAITING state transitions."""
import pytest
from state_machine import FactoryDB, JobStatus, TRANSITIONS

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"

@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)

def test_waiting_status_exists():
    assert JobStatus.WAITING == "waiting"

def test_planning_can_transition_to_waiting():
    assert JobStatus.WAITING in TRANSITIONS[JobStatus.PLANNING]

def test_waiting_can_transition_to_implementing():
    assert JobStatus.IMPLEMENTING in TRANSITIONS[JobStatus.WAITING]

def test_waiting_can_transition_to_failed():
    assert JobStatus.FAILED in TRANSITIONS[JobStatus.WAITING]

def test_transition_planning_to_waiting(db):
    job_id = db.create_job(project_slug="devbrain", title="Test WAITING", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    job = db.transition(job_id, JobStatus.WAITING)
    assert job.status == JobStatus.WAITING

def test_factory_job_has_submitted_by_field(db):
    """FactoryJob dataclass includes submitted_by and blocked_by_job_id."""
    job_id = db.create_job(project_slug="devbrain", title="Test fields", spec="Test")
    job = db.get_job(job_id)
    # Default values — job not submitted_by anyone yet
    assert hasattr(job, "submitted_by")
    assert hasattr(job, "blocked_by_job_id")
    assert job.submitted_by is None
    assert job.blocked_by_job_id is None
