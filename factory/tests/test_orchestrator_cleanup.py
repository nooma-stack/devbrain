"""Tests for cleanup agent integration into orchestrator."""
import pytest
from state_machine import FactoryDB, JobStatus
from cleanup_agent import CleanupAgent, CleanupReport

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture
def agent(db):
    return CleanupAgent(db)


class TestCleanupAgentImport:
    """Verify the CleanupAgent can be imported into orchestrator."""

    def test_import_cleanup_agent(self):
        from cleanup_agent import CleanupAgent as CA
        assert CA is not None

    def test_import_from_orchestrator_module(self):
        """Verify orchestrator imports CleanupAgent without error."""
        import orchestrator
        assert hasattr(orchestrator, 'CleanupAgent') or 'CleanupAgent' in dir(orchestrator)


class TestCleanupAgentMethods:
    """Verify CleanupAgent has the expected public API."""

    def test_has_attempt_recovery(self, agent):
        assert callable(getattr(agent, 'attempt_recovery', None))

    def test_has_run_post_cleanup(self, agent):
        assert callable(getattr(agent, 'run_post_cleanup', None))

    def test_attempt_recovery_returns_cleanup_report(self, db, agent):
        """attempt_recovery should return a CleanupReport dataclass."""
        job_id = db.create_job(
            project_slug="devbrain",
            title="Recovery API test",
            spec="Test spec",
        )
        db.transition(job_id, JobStatus.PLANNING)
        db.store_artifact(job_id, "planning", "plan", "A plan.")
        db.transition(job_id, JobStatus.IMPLEMENTING)
        db.store_artifact(job_id, "implementing", "diff", "some diff")
        db.transition(job_id, JobStatus.REVIEWING)
        db.store_artifact(
            job_id, "reviewing", "review",
            "BLOCKING: missing error handling",
            findings_count=1, blocking_count=1,
        )
        db.transition(job_id, JobStatus.FIX_LOOP)
        db.transition(job_id, JobStatus.FAILED)
        job = db.get_job(job_id)
        report = agent.attempt_recovery(job)
        assert isinstance(report, CleanupReport)
        assert report.outcome in ("recovered", "fix_attempted", "needs_human")


class TestOrchestratorCleanupIntegration:
    """Integration test: walk a job to FAILED, verify cleanup report exists."""

    def test_post_cleanup_creates_report_for_failed_job(self, db, agent):
        job_id = db.create_job(
            project_slug="devbrain",
            title="Orchestrator cleanup integration test",
            spec="Test that post-cleanup stores a report",
        )
        # Walk to FAILED
        db.transition(job_id, JobStatus.PLANNING)
        db.store_artifact(job_id, "planning", "plan", "A plan.")
        db.transition(job_id, JobStatus.IMPLEMENTING)
        db.store_artifact(job_id, "implementing", "diff", "diff content")
        db.transition(job_id, JobStatus.REVIEWING)
        db.store_artifact(
            job_id, "reviewing", "review",
            "BLOCKING: critical issue",
            findings_count=1, blocking_count=1,
        )
        db.transition(job_id, JobStatus.FIX_LOOP)
        db.transition(job_id, JobStatus.FAILED)

        # Run post-cleanup (same as orchestrator would call)
        agent.run_post_cleanup(job_id)

        # Verify report exists in DB
        reports = db.get_cleanup_reports(job_id)
        assert len(reports) >= 1
        latest = reports[-1]
        assert latest["report_type"] == "post_run"
        assert latest["outcome"] == "failed"

    def test_post_cleanup_creates_report_for_approved_job(self, db, agent):
        job_id = db.create_job(
            project_slug="devbrain",
            title="Orchestrator cleanup approved test",
            spec="Test that post-cleanup stores a report for success",
        )
        # Walk to APPROVED
        db.transition(job_id, JobStatus.PLANNING)
        db.store_artifact(job_id, "planning", "plan", "A plan.")
        db.transition(job_id, JobStatus.IMPLEMENTING)
        db.store_artifact(job_id, "implementing", "diff", "diff content")
        db.transition(job_id, JobStatus.REVIEWING)
        db.store_artifact(
            job_id, "reviewing", "review", "LGTM",
            findings_count=0, blocking_count=0,
        )
        db.transition(job_id, JobStatus.QA)
        db.store_artifact(
            job_id, "qa", "qa_report", "All pass",
            findings_count=0, blocking_count=0,
        )
        db.transition(job_id, JobStatus.READY_FOR_APPROVAL)
        db.transition(job_id, JobStatus.APPROVED)

        agent.run_post_cleanup(job_id)

        reports = db.get_cleanup_reports(job_id)
        assert len(reports) >= 1
        latest = reports[-1]
        assert latest["report_type"] == "post_run"
        assert latest["outcome"] == "clean"

    def test_recovery_report_stored_in_db(self, db, agent):
        """Simulate what orchestrator does: call attempt_recovery, store the report."""
        job_id = db.create_job(
            project_slug="devbrain",
            title="Recovery store test",
            spec="Test that recovery reports are persisted",
        )
        db.transition(job_id, JobStatus.PLANNING)
        db.store_artifact(job_id, "planning", "plan", "A plan.")
        db.transition(job_id, JobStatus.IMPLEMENTING)
        db.store_artifact(job_id, "implementing", "diff", "diff")
        db.transition(job_id, JobStatus.REVIEWING)
        db.store_artifact(
            job_id, "reviewing", "review",
            "BLOCKING: issue here",
            findings_count=1, blocking_count=1,
        )
        db.transition(job_id, JobStatus.FIX_LOOP)
        db.transition(job_id, JobStatus.FAILED)
        job = db.get_job(job_id)

        # Call attempt_recovery and store report (mirrors orchestrator logic)
        recovery_report = agent.attempt_recovery(job)
        db.store_cleanup_report(
            job_id=job.id,
            report_type=recovery_report.report_type,
            outcome=recovery_report.outcome,
            summary=recovery_report.summary,
            phases_traversed=recovery_report.phases_traversed,
            artifacts_summary=recovery_report.artifacts_summary,
            recovery_diagnosis=recovery_report.recovery_diagnosis,
            recovery_action_taken=recovery_report.recovery_action_taken,
            time_elapsed_seconds=recovery_report.time_elapsed_seconds,
        )

        reports = db.get_cleanup_reports(job_id)
        recovery_reports = [r for r in reports if r["report_type"] == "recovery"]
        assert len(recovery_reports) >= 1
        assert recovery_reports[-1]["recovery_diagnosis"] is not None
