"""Tests for cleanup_agent — post-run housekeeping and recovery."""
import pytest
from state_machine import FactoryDB, JobStatus
from cleanup_agent import CleanupAgent, CleanupReport

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
                "Test approved job",
                "Test failed job",
                "Recovery test job",
                "Lock release test task6",
                "No locks task6",
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


@pytest.fixture
def agent(db):
    return CleanupAgent(db)


@pytest.fixture
def approved_job(db):
    """Simulate a job that went through the full happy path to APPROVED."""
    job_id = db.create_job(
        project_slug="devbrain",
        title="Test approved job",
        spec="Implement widget feature",
    )
    # Walk through the pipeline
    db.transition(job_id, JobStatus.PLANNING)
    db.store_artifact(job_id, "planning", "plan", "Build the widget per spec.")
    db.transition(job_id, JobStatus.IMPLEMENTING)
    db.store_artifact(job_id, "implementing", "diff", "--- a/widget.py\n+++ b/widget.py\n+class Widget: pass")
    db.transition(job_id, JobStatus.REVIEWING)
    db.store_artifact(job_id, "reviewing", "review", "LGTM, no blocking issues.", findings_count=0, blocking_count=0)
    db.transition(job_id, JobStatus.QA)
    db.store_artifact(job_id, "qa", "qa_report", "All checks pass.", findings_count=0, blocking_count=0)
    db.transition(job_id, JobStatus.READY_FOR_APPROVAL)
    db.transition(job_id, JobStatus.APPROVED)
    return job_id


@pytest.fixture
def failed_job(db):
    """Simulate a job that hit blocking review findings and eventually failed."""
    job_id = db.create_job(
        project_slug="devbrain",
        title="Test failed job",
        spec="Implement broken feature",
        metadata={"branch": "factory/broken-feature"},
    )
    db.transition(job_id, JobStatus.PLANNING)
    db.store_artifact(job_id, "planning", "plan", "Plan for broken feature.")
    db.transition(job_id, JobStatus.IMPLEMENTING)
    db.store_artifact(job_id, "implementing", "diff", "--- a/broken.py\n+++ b/broken.py\n+raise RuntimeError")
    db.transition(job_id, JobStatus.REVIEWING)
    db.store_artifact(
        job_id, "reviewing", "review",
        "Blocking: unhandled exception path.",
        findings_count=3, blocking_count=2,
    )
    db.transition(job_id, JobStatus.FIX_LOOP)
    db.store_artifact(job_id, "fix_loop", "fix_attempt", "Attempted fix but still broken.")
    db.transition(job_id, JobStatus.FAILED)
    return job_id


class TestPostRunReportSuccess:
    def test_outcome_is_clean(self, agent, approved_job):
        report = agent.run_post_cleanup(approved_job)
        assert report["outcome"] == "clean"

    def test_report_type(self, agent, approved_job):
        report = agent.run_post_cleanup(approved_job)
        assert report["report_type"] == "post_run"

    def test_phases_traversed_not_empty(self, agent, approved_job):
        report = agent.run_post_cleanup(approved_job)
        assert len(report["phases_traversed"]) > 0
        assert "planning" in report["phases_traversed"]

    def test_artifacts_summary_has_counts(self, agent, approved_job):
        report = agent.run_post_cleanup(approved_job)
        assert "total_artifacts" in report["artifacts_summary"]
        assert report["artifacts_summary"]["total_artifacts"] >= 4


class TestPostRunReportFailed:
    def test_outcome_is_failed(self, agent, failed_job):
        report = agent.run_post_cleanup(failed_job)
        assert report["outcome"] == "failed"

    def test_summary_mentions_failure(self, agent, failed_job):
        report = agent.run_post_cleanup(failed_job)
        # Summary should contain some failure information
        assert "fail" in report["summary"].lower() or "error" in report["summary"].lower()

    def test_phases_include_fix_loop(self, agent, failed_job):
        report = agent.run_post_cleanup(failed_job)
        assert "fix_loop" in report["phases_traversed"]


class TestPostRunArchivesJob:
    def test_archived_at_set(self, agent, db, approved_job):
        agent.run_post_cleanup(approved_job)
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT archived_at FROM devbrain.factory_jobs WHERE id = %s",
                (approved_job,),
            )
            assert cur.fetchone()[0] is not None

    def test_failed_job_archived(self, agent, db, failed_job):
        agent.run_post_cleanup(failed_job)
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT archived_at FROM devbrain.factory_jobs WHERE id = %s",
                (failed_job,),
            )
            assert cur.fetchone()[0] is not None


class TestPostRunStoresReportInDB:
    def test_report_persisted(self, agent, db, approved_job):
        agent.run_post_cleanup(approved_job)
        reports = db.get_cleanup_reports(approved_job)
        assert len(reports) >= 1
        latest = reports[-1]
        assert latest["report_type"] == "post_run"
        assert latest["outcome"] == "clean"

    def test_failed_report_persisted(self, agent, db, failed_job):
        agent.run_post_cleanup(failed_job)
        reports = db.get_cleanup_reports(failed_job)
        assert len(reports) >= 1
        latest = reports[-1]
        assert latest["report_type"] == "post_run"
        assert latest["outcome"] == "failed"


class TestAttemptRecovery:
    def test_returns_cleanup_report(self, agent, db):
        """attempt_recovery returns a CleanupReport dataclass."""
        job_id = db.create_job(
            project_slug="devbrain",
            title="Recovery test job",
            spec="Test spec for recovery",
        )
        db.transition(job_id, JobStatus.PLANNING)
        db.store_artifact(job_id, "planning", "plan", "A plan.")
        db.transition(job_id, JobStatus.IMPLEMENTING)
        db.store_artifact(job_id, "implementing", "diff", "some diff")
        db.transition(job_id, JobStatus.REVIEWING)
        db.store_artifact(
            job_id, "reviewing", "review",
            "Blocking: critical security flaw",
            findings_count=1, blocking_count=1,
        )
        db.transition(job_id, JobStatus.FIX_LOOP)
        db.transition(job_id, JobStatus.FAILED)
        job = db.get_job(job_id)
        report = agent.attempt_recovery(job)
        assert isinstance(report, CleanupReport)
        assert report.job_id == job_id
        assert report.report_type == "recovery"
        assert report.recovery_diagnosis is not None


def test_post_cleanup_releases_file_locks(db, agent):
    """Post-run cleanup releases all file locks held by the job."""
    from file_registry import FileRegistry

    registry = FileRegistry(db)

    # Create a failed job
    job_id = db.create_job(project_slug="devbrain", title="Lock release test task6", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.FAILED)

    job = db.get_job(job_id)
    registry.acquire_locks(
        job_id=job.id,
        project_id=job.project_id,
        file_paths=["src/cleanup_lock_a_task6.py", "src/cleanup_lock_b_task6.py"],
        dev_id="alice",
    )

    # Verify locks exist
    assert len(registry.get_job_locks(job.id)) == 2

    # Run cleanup
    agent.run_post_cleanup(job.id)

    # Verify locks released
    assert len(registry.get_job_locks(job.id)) == 0


def test_cleanup_handles_jobs_with_no_locks(db, agent):
    """Cleanup agent handles jobs that don't have any locks (no crash)."""
    job_id = db.create_job(project_slug="devbrain", title="No locks task6", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.FAILED)

    # Should complete without error even though no locks exist
    report = agent.run_post_cleanup(job_id)
    assert report["outcome"] == "failed"
