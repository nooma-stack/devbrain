"""Cleanup agent — post-run housekeeping and on-error recovery.

Two modes:
1. Post-run cleanup: runs after every terminal state, summarizes the job,
   archives it, and stores a cleanup report.
2. On-error recovery: gets one focused attempt to diagnose and fix a failure
   before the job transitions to FAILED.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from file_registry import FileRegistry
from notifications.router import NotificationRouter, NotificationEvent
from state_machine import FactoryDB, FactoryJob, JobStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "devbrain.yaml"
with open(_CONFIG_PATH) as _f:
    _config = yaml.safe_load(_f)
_CLEANUP_CONFIG = _config.get("factory", {}).get("cleanup", {})

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Timing configuration — from config with defaults
SOFT_TIMER_SECONDS = _CLEANUP_CONFIG.get("soft_timer_seconds", 600)
EXTENSION_SECONDS = _CLEANUP_CONFIG.get("extension_seconds", 300)
HARD_CEILING_SECONDS = _CLEANUP_CONFIG.get("hard_ceiling_seconds", 1800)
BRANCH_CLEANUP_ENABLED = _CLEANUP_CONFIG.get("branch_cleanup", True)

TERMINAL_STATES = {JobStatus.APPROVED, JobStatus.REJECTED, JobStatus.DEPLOYED, JobStatus.FAILED}
SUCCESS_STATES = {JobStatus.APPROVED, JobStatus.DEPLOYED, JobStatus.READY_FOR_APPROVAL}

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ProgressCheckpoint:
    timestamp: str
    action: str
    result: str
    progress_made: bool


@dataclass
class CleanupReport:
    job_id: str
    report_type: str
    outcome: str
    summary: str
    phases_traversed: list[str] = field(default_factory=list)
    artifacts_summary: dict[str, Any] = field(default_factory=dict)
    recovery_diagnosis: str | None = None
    recovery_action_taken: str | None = None
    time_elapsed_seconds: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# CleanupAgent
# ---------------------------------------------------------------------------


class CleanupAgent:
    """Handles post-run housekeeping and on-error recovery attempts."""

    def __init__(self, db: FactoryDB):
        self.db = db

    def _event_type_for_status(self, status: JobStatus) -> str | None:
        """Map a terminal status to a notification event_type. Returns None for silent transitions."""
        return {
            JobStatus.READY_FOR_APPROVAL: "job_ready",
            JobStatus.FAILED: "job_failed",
        }.get(status)

    def _notification_title(self, job: FactoryJob, event_type: str) -> str:
        """Build a notification title from job + event."""
        return {
            "job_ready": f"✅ Job ready for review: {job.title}",
            "job_failed": f"❌ Job failed: {job.title}",
            "unblocked": f"🔓 Job unblocked: {job.title}",
            "needs_human": f"🤔 Job needs human input: {job.title}",
        }.get(event_type, f"Job update: {job.title}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_post_cleanup(self, job_id: str) -> dict:
        """Mode 1 — called after every terminal state.

        Returns the cleanup report as a plain dict.
        """
        start = time.monotonic()
        job = self.db.get_job(job_id)
        if job is None:
            raise ValueError(f"Job {job_id} not found")

        artifacts = self.db.get_artifacts(job_id)
        artifacts_summary = self._summarize_artifacts(artifacts)
        phases = self._extract_phases(artifacts)
        is_success = job.status in SUCCESS_STATES

        if is_success:
            outcome = "clean"
            summary = self._build_success_summary(job, phases, artifacts_summary)
        else:
            outcome = "failed"
            summary = self._build_failure_summary(job, phases, artifacts_summary)

        # Best-effort branch cleanup for failed/rejected jobs
        if job.status in {JobStatus.FAILED, JobStatus.REJECTED}:
            self._cleanup_branch(job)

        elapsed = int(time.monotonic() - start)

        # Persist to DB
        self.db.store_cleanup_report(
            job_id=job_id,
            report_type="post_run",
            outcome=outcome,
            summary=summary,
            phases_traversed=phases,
            artifacts_summary=artifacts_summary,
            time_elapsed_seconds=elapsed,
        )

        report = CleanupReport(
            job_id=job_id,
            report_type="post_run",
            outcome=outcome,
            summary=summary,
            phases_traversed=phases,
            artifacts_summary=artifacts_summary,
            time_elapsed_seconds=elapsed,
        )

        # Fire notification for this job's terminal state.
        # Done BEFORE archive/lock release so that transient states like
        # READY_FOR_APPROVAL (which cannot be archived) still dispatch.
        try:
            router = NotificationRouter(self.db)
            event_type = self._event_type_for_status(job.status)
            if event_type and job.submitted_by:
                event = NotificationEvent(
                    event_type=event_type,
                    recipient_dev_id=job.submitted_by,
                    title=self._notification_title(job, event_type),
                    body=report.summary,
                    job_id=job.id,
                    metadata={
                        "final_status": job.status.value,
                        "error_count": job.error_count,
                    },
                )
                router.send(event)
        except Exception as e:
            logger.warning(
                "Notification dispatch failed for job %s: %s (non-blocking)",
                job_id[:8], e,
            )

        # Archive the job (only if in a terminal state the DB accepts)
        try:
            self.db.archive_job(job_id)
        except ValueError as e:
            logger.debug(
                "Skipping archive for job %s: %s (non-terminal status)",
                job_id[:8], e,
            )

        # Release file locks held by this job
        try:
            registry = FileRegistry(self.db)
            released = registry.release_locks(job_id)
            if released > 0:
                logger.info(
                    "Cleanup released %d file locks for job %s",
                    released, job_id[:8],
                )
            # Also clean up any expired locks globally (safety net)
            registry.cleanup_expired_locks()
        except Exception as e:
            logger.warning(
                "File lock cleanup failed for job %s: %s (non-blocking)",
                job_id[:8], e,
            )

        return report.to_dict()

    def attempt_recovery(self, job: FactoryJob) -> CleanupReport:
        """Mode 2 — called before a job transitions to FAILED.

        Gets one focused attempt to diagnose and possibly fix the failure.
        Returns a CleanupReport dataclass.
        """
        start = time.monotonic()
        artifacts = self.db.get_artifacts(job.id)
        diagnosis = self._diagnose_failure(artifacts)
        converging = self._check_fix_convergence(artifacts)

        recovery_action: str | None = None
        outcome: str

        if diagnosis["category"] == "qa_failure" and converging:
            # Attempt a targeted fix
            fix_result = self._attempt_targeted_fix(job, diagnosis)
            recovery_action = fix_result
            outcome = "fix_attempted"
        else:
            # Not fixable automatically — needs human attention
            questions = self._build_human_questions(diagnosis)
            recovery_action = f"needs_human: {questions}"
            outcome = "needs_human"

        elapsed = int(time.monotonic() - start)
        phases = self._extract_phases(artifacts)

        report = CleanupReport(
            job_id=job.id,
            report_type="recovery",
            outcome=outcome,
            summary=f"Recovery attempt for job '{job.title}': {outcome}",
            phases_traversed=phases,
            artifacts_summary=self._summarize_artifacts(artifacts),
            recovery_diagnosis=diagnosis["description"],
            recovery_action_taken=recovery_action,
            time_elapsed_seconds=elapsed,
        )

        if outcome == "needs_human":
            # Fire needs_human notification
            try:
                router = NotificationRouter(self.db)
                if job.submitted_by:
                    router.send(NotificationEvent(
                        event_type="needs_human",
                        recipient_dev_id=job.submitted_by,
                        title=f"🤔 Job needs human input: {job.title}",
                        body=report.summary,
                        job_id=job.id,
                    ))
            except Exception as e:
                logger.warning("needs_human notification failed: %s", e)

        return report

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _diagnose_failure(self, artifacts: list[dict]) -> dict:
        """Categorise the failure from artifact history.

        Categories: qa_failure, persistent_blocking, unknown.
        """
        review_artifacts = [a for a in artifacts if a["phase"] in ("reviewing", "qa")]
        fix_artifacts = [a for a in artifacts if a["phase"] == "fix_loop"]

        total_blocking = sum(a.get("blocking_count", 0) for a in review_artifacts)
        has_fixes = len(fix_artifacts) > 0

        if total_blocking > 0 and has_fixes:
            return {
                "category": "qa_failure",
                "description": (
                    f"QA/review blocking issues ({total_blocking} blocking findings) "
                    f"with {len(fix_artifacts)} fix attempt(s)."
                ),
                "blocking_count": total_blocking,
                "fix_attempts": len(fix_artifacts),
            }
        elif total_blocking > 0:
            return {
                "category": "persistent_blocking",
                "description": (
                    f"Persistent blocking issues ({total_blocking} blocking findings) "
                    "with no fix attempts recorded."
                ),
                "blocking_count": total_blocking,
            }
        else:
            last_content = artifacts[-1]["content"] if artifacts else "No artifacts"
            return {
                "category": "unknown",
                "description": f"Unknown failure. Last artifact: {last_content[:200]}",
            }

    def _check_fix_convergence(self, artifacts: list[dict]) -> bool:
        """Check whether fix attempts are making progress.

        Looks at successive review/qa blocking counts to see if they decrease.
        """
        review_arts = [
            a for a in artifacts
            if a["phase"] in ("reviewing", "qa") and a.get("blocking_count", 0) > 0
        ]
        if len(review_arts) < 2:
            # Not enough data to judge convergence; assume not converging
            return False

        counts = [a["blocking_count"] for a in review_arts]
        # Converging if the latest count is strictly less than the first
        return counts[-1] < counts[0]

    def _attempt_targeted_fix(self, job: FactoryJob, diagnosis: dict) -> str:
        """Run a targeted fix via CLI executor.

        Returns a description of the action taken.
        """
        try:
            from cli_executor import run_cli, DEFAULT_CLI_ASSIGNMENTS

            cli_name = job.assigned_cli or DEFAULT_CLI_ASSIGNMENTS.get("fix", "claude")
            project_root = self._get_project_root(job)

            prompt = (
                f"This job failed with the following diagnosis:\n"
                f"{diagnosis['description']}\n\n"
                f"Please apply a minimal, targeted fix to resolve the blocking issues."
            )
            result = run_cli(cli_name, prompt, cwd=project_root)
            if result.success:
                return f"Targeted fix applied via {cli_name}. stdout: {result.stdout[:500]}"
            else:
                return f"Fix attempt via {cli_name} failed (exit {result.exit_code}): {result.stderr[:500]}"
        except Exception as exc:
            logger.error("Targeted fix failed: %s", exc)
            return f"Fix attempt raised exception: {exc}"

    def _get_project_root(self, job: FactoryJob) -> str:
        """Derive the project working directory for a job."""
        # Use metadata if available, otherwise fall back to a conventional path
        if "project_root" in job.metadata:
            return job.metadata["project_root"]
        return f"/Users/patrickkelly/{job.project_slug}"

    def _summarize_artifacts(self, artifacts: list[dict]) -> dict:
        """Build a summary dict of artifacts grouped by phase."""
        by_phase: dict[str, int] = {}
        total_findings = 0
        total_blocking = 0
        for a in artifacts:
            by_phase[a["phase"]] = by_phase.get(a["phase"], 0) + 1
            total_findings += a.get("findings_count", 0)
            total_blocking += a.get("blocking_count", 0)

        return {
            "total_artifacts": len(artifacts),
            "by_phase": by_phase,
            "total_findings": total_findings,
            "total_blocking": total_blocking,
        }

    def _extract_phases(self, artifacts: list[dict]) -> list[str]:
        """Return an ordered, deduplicated list of phases from artifacts."""
        seen: set[str] = set()
        phases: list[str] = []
        for a in artifacts:
            phase = a["phase"]
            if phase not in seen:
                seen.add(phase)
                phases.append(phase)
        return phases

    def _build_success_summary(
        self, job: FactoryJob, phases: list[str], artifacts_summary: dict
    ) -> str:
        return (
            f"Job '{job.title}' completed successfully (status: {job.status.value}). "
            f"Phases: {' -> '.join(phases)}. "
            f"Artifacts: {artifacts_summary['total_artifacts']}, "
            f"findings: {artifacts_summary['total_findings']}, "
            f"blocking: {artifacts_summary['total_blocking']}."
        )

    def _build_failure_summary(
        self, job: FactoryJob, phases: list[str], artifacts_summary: dict
    ) -> str:
        return (
            f"Job '{job.title}' failed (status: {job.status.value}, "
            f"error_count: {job.error_count}). "
            f"Phases: {' -> '.join(phases)}. "
            f"Artifacts: {artifacts_summary['total_artifacts']}, "
            f"findings: {artifacts_summary['total_findings']}, "
            f"blocking: {artifacts_summary['total_blocking']}."
        )

    def _cleanup_branch(self, job: FactoryJob) -> None:
        """Best-effort delete the git branch for failed/rejected jobs."""
        if not BRANCH_CLEANUP_ENABLED:
            logger.debug("Branch cleanup disabled by config for job %s", job.id[:8])
            return

        branch = job.branch_name or job.metadata.get("branch")
        if not branch:
            logger.debug("No branch to clean up for job %s", job.id[:8])
            return

        project_root = self._get_project_root(job)
        try:
            subprocess.run(
                ["git", "branch", "-d", branch],
                cwd=project_root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            logger.info("Cleaned up branch %s for job %s", branch, job.id[:8])
        except Exception as exc:
            logger.debug("Branch cleanup failed (best-effort): %s", exc)

    def _build_human_questions(self, diagnosis: dict) -> str:
        """Generate questions for human review based on the diagnosis."""
        category = diagnosis["category"]
        if category == "persistent_blocking":
            return (
                "1. Are the blocking findings valid or false positives? "
                "2. Does the spec need to be revised? "
                "3. Should this job be re-queued with a different approach?"
            )
        return (
            "1. What caused this failure? "
            "2. Can the spec be clarified? "
            "3. Should this be retried or cancelled?"
        )
