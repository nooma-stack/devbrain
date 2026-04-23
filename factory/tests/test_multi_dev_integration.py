"""End-to-end integration test for multi-dev file locking."""
import pytest
from state_machine import FactoryDB, JobStatus
from file_registry import FileRegistry
from cleanup_agent import CleanupAgent

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
                "Independent A integ",
                "Independent B integ",
                "Shared A integ",
                "Shared B integ",
                "Release test A integ",
                "Release test B integ",
                "Crashed job integ",
                "New job integ",
                "Blocker A integ",
                "Blocked B integ",
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


def test_two_jobs_independent_files_both_proceed(db):
    """Two jobs with no file overlap both acquire locks successfully."""
    registry = FileRegistry(db)

    job_a = db.get_job(db.create_job(project_slug="devbrain", title="Independent A integ", spec="Test"))
    job_b = db.get_job(db.create_job(project_slug="devbrain", title="Independent B integ", spec="Test"))

    result_a = registry.acquire_locks(
        job_a.id, job_a.project_id,
        ["src/integ_feat_a/one.py", "src/integ_feat_a/two.py"],
        dev_id="alice",
    )
    result_b = registry.acquire_locks(
        job_b.id, job_b.project_id,
        ["src/integ_feat_b/one.py", "src/integ_feat_b/two.py"],
        dev_id="bob",
    )

    assert result_a.success is True
    assert result_b.success is True

    # Cleanup
    registry.release_locks(job_a.id)
    registry.release_locks(job_b.id)


def test_two_jobs_shared_file_second_blocks(db):
    """Two jobs touching the same file: first wins, second blocks."""
    registry = FileRegistry(db)

    job_a = db.get_job(db.create_job(project_slug="devbrain", title="Shared A integ", spec="Test"))
    job_b = db.get_job(db.create_job(project_slug="devbrain", title="Shared B integ", spec="Test"))

    result_a = registry.acquire_locks(
        job_a.id, job_a.project_id,
        ["src/integ_shared_common.py"],
        dev_id="alice",
    )
    result_b = registry.acquire_locks(
        job_b.id, job_b.project_id,
        ["src/integ_shared_common.py"],
        dev_id="bob",
    )

    assert result_a.success is True
    assert result_b.success is False
    assert result_b.conflicts[0]["file_path"] == "src/integ_shared_common.py"
    assert result_b.conflicts[0]["blocking_job_id"] == job_a.id

    # Cleanup
    registry.release_locks(job_a.id)


def test_cleanup_releases_then_blocked_job_proceeds(db):
    """After first job's cleanup, blocked job can acquire locks."""
    registry = FileRegistry(db)
    cleanup = CleanupAgent(db)

    # Job A acquires lock on shared file
    job_a_id = db.create_job(project_slug="devbrain", title="Release test A integ", spec="Test")
    db.transition(job_a_id, JobStatus.PLANNING)
    job_a = db.get_job(job_a_id)
    registry.acquire_locks(
        job_a.id, job_a.project_id,
        ["src/integ_release_test_shared.py"],
        dev_id="alice",
    )

    # Job B tries same file — blocked
    job_b_id = db.create_job(project_slug="devbrain", title="Release test B integ", spec="Test")
    job_b = db.get_job(job_b_id)
    result = registry.acquire_locks(
        job_b.id, job_b.project_id,
        ["src/integ_release_test_shared.py"],
        dev_id="bob",
    )
    assert result.success is False

    # Job A completes and cleanup runs
    db.transition(job_a_id, JobStatus.FAILED)
    cleanup.run_post_cleanup(job_a_id)

    # Job B can now acquire
    result = registry.acquire_locks(
        job_b.id, job_b.project_id,
        ["src/integ_release_test_shared.py"],
        dev_id="bob",
    )
    assert result.success is True

    # Cleanup for job B
    db.transition(job_b_id, JobStatus.PLANNING)
    db.transition(job_b_id, JobStatus.FAILED)
    cleanup.run_post_cleanup(job_b_id)


def test_expired_locks_get_cleaned_up_automatically(db):
    """A crashed job's expired locks get cleaned up when another job tries to acquire."""
    registry = FileRegistry(db)

    # Simulate a crashed job by acquiring and manually expiring the lock
    crashed_job_id = db.create_job(project_slug="devbrain", title="Crashed job integ", spec="Test")
    crashed_job = db.get_job(crashed_job_id)
    registry.acquire_locks(
        crashed_job.id, crashed_job.project_id,
        ["src/integ_crashed_file.py"],
        dev_id="alice",
    )
    # Force expiration
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.file_locks SET expires_at = now() - interval '1 hour' WHERE job_id = %s",
            (crashed_job.id,),
        )
        conn.commit()

    # New job should be able to acquire the same file (expired locks auto-cleaned in acquire_locks)
    new_job_id = db.create_job(project_slug="devbrain", title="New job integ", spec="Test")
    new_job = db.get_job(new_job_id)
    result = registry.acquire_locks(
        new_job.id, new_job.project_id,
        ["src/integ_crashed_file.py"],
        dev_id="bob",
    )
    assert result.success is True

    registry.release_locks(new_job.id)


def test_blocked_job_has_blocked_by_set(db):
    """A job transitioned to BLOCKED has blocked_by_job_id populated."""
    registry = FileRegistry(db)

    job_a = db.get_job(db.create_job(project_slug="devbrain", title="Blocker A integ", spec="Test"))
    registry.acquire_locks(
        job_a.id, job_a.project_id,
        ["src/integ_blocker.py"],
        dev_id="alice",
    )

    job_b_id = db.create_job(project_slug="devbrain", title="Blocked B integ", spec="Test")
    result = registry.acquire_locks(
        job_b_id, job_a.project_id,
        ["src/integ_blocker.py"],
        dev_id="bob",
    )
    assert result.success is False
    assert result.conflicts[0]["blocking_job_id"] == job_a.id

    # Simulate the orchestrator setting blocked_by_job_id
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.factory_jobs SET blocked_by_job_id = %s WHERE id = %s",
            (job_a.id, job_b_id),
        )
        conn.commit()
    db.transition(job_b_id, JobStatus.PLANNING)
    db.transition(job_b_id, JobStatus.BLOCKED)

    job_b = db.get_job(job_b_id)
    assert job_b.status == JobStatus.BLOCKED
    assert job_b.blocked_by_job_id == job_a.id

    # Cleanup
    registry.release_locks(job_a.id)
