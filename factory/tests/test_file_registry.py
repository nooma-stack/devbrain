"""Tests for the file lock registry."""
import pytest
from state_machine import FactoryDB, JobStatus
from file_registry import FileRegistry, LockConflict

from config import DATABASE_URL

@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)

@pytest.fixture
def registry(db):
    return FileRegistry(db)

@pytest.fixture
def job1(db):
    job_id = db.create_job(project_slug="devbrain", title="Test job 1", spec="Test")
    return db.get_job(job_id)

@pytest.fixture
def job2(db):
    job_id = db.create_job(project_slug="devbrain", title="Test job 2", spec="Test")
    return db.get_job(job_id)

def test_acquire_locks_no_conflicts(registry, job1):
    files = ["src/auth_test_task3.py", "tests/test_auth_task3.py"]
    result = registry.acquire_locks(job1.id, job1.project_id, files, dev_id="alice")
    assert result.success is True
    assert result.conflicts == []
    # Cleanup
    registry.release_locks(job1.id)

def test_acquire_locks_detects_conflicts(registry, job1, job2):
    files = ["src/shared_task3.py", "src/only_job1_task3.py"]
    registry.acquire_locks(job1.id, job1.project_id, files, dev_id="alice")
    result = registry.acquire_locks(
        job2.id, job2.project_id,
        ["src/shared_task3.py", "src/only_job2_task3.py"],
        dev_id="bob",
    )
    assert result.success is False
    assert len(result.conflicts) == 1
    assert result.conflicts[0]["file_path"] == "src/shared_task3.py"
    assert result.conflicts[0]["blocking_job_id"] == job1.id
    # Cleanup
    registry.release_locks(job1.id)

def test_release_locks(registry, job1):
    files = ["src/foo_task3.py", "src/bar_task3.py"]
    registry.acquire_locks(job1.id, job1.project_id, files, dev_id="alice")
    released_count = registry.release_locks(job1.id)
    assert released_count == 2

def test_release_unblocks_waiting(registry, job1, job2):
    files = ["src/shared_unblock_task3.py"]
    registry.acquire_locks(job1.id, job1.project_id, files, dev_id="alice")
    result = registry.acquire_locks(job2.id, job2.project_id, files, dev_id="bob")
    assert result.success is False
    registry.release_locks(job1.id)
    result2 = registry.acquire_locks(job2.id, job2.project_id, files, dev_id="bob")
    assert result2.success is True
    registry.release_locks(job2.id)

def test_expired_locks_cleanup(registry, db, job1):
    files = ["src/old_task3.py"]
    registry.acquire_locks(job1.id, job1.project_id, files, dev_id="alice")
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.file_locks SET expires_at = now() - interval '1 hour' WHERE job_id = %s",
            (job1.id,),
        )
        conn.commit()
    cleaned = registry.cleanup_expired_locks()
    assert cleaned >= 1

def test_list_locked_files_for_project(registry, job1):
    files = ["src/a_task3.py", "src/b_task3.py", "src/c_task3.py"]
    registry.acquire_locks(job1.id, job1.project_id, files, dev_id="alice")
    locked = registry.list_locked_files(job1.project_id)
    locked_paths = [f["file_path"] for f in locked if f["job_id"] == job1.id]
    assert "src/a_task3.py" in locked_paths
    assert "src/b_task3.py" in locked_paths
    assert "src/c_task3.py" in locked_paths
    registry.release_locks(job1.id)

def test_get_job_locks(registry, job1):
    files = ["src/xyz_task3.py"]
    registry.acquire_locks(job1.id, job1.project_id, files, dev_id="alice")
    locks = registry.get_job_locks(job1.id)
    assert "src/xyz_task3.py" in locks
    registry.release_locks(job1.id)
