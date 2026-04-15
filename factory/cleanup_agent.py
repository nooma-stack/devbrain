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

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import CLEANUP_CONFIG as _CLEANUP_CONFIG, project_path as _project_path  # noqa: E402

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
            "job_started": f"🚀 Job started: {job.title}",
            "job_ready": f"✅ Job ready for review: {job.title}",
            "job_failed": f"❌ Job failed: {job.title}",
            "blocked": f"🔒 Job blocked: {job.title}",
            "unblocked": f"🔓 Job unblocked: {job.title}",
            "needs_human": f"🤔 Job needs human input: {job.title}",
            "recovery_started": f"🛠 Recovery started: {job.title}",
            "recovery_succeeded": f"🎉 Recovery succeeded: {job.title}",
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

        # Fire recovery_started notification so the dev knows we're trying
        self._fire_notification(
            job,
            event_type="recovery_started",
            body=(
                f"Your job hit max fix retries. Recovery agent is attempting "
                f"to diagnose and fix the failure now."
            ),
        )

        artifacts = self.db.get_artifacts(job.id)
        diagnosis = self._diagnose_failure(artifacts)
        converging = self._check_fix_convergence(artifacts)

        recovery_action: str | None = None
        outcome: str

        if diagnosis["category"] == "qa_failure" and converging:
            # Attempt a targeted fix
            fix_success, fix_description = self._attempt_targeted_fix(job, diagnosis)
            recovery_action = fix_description
            outcome = "recovered" if fix_success else "failed"
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

        if outcome == "recovered":
            self._fire_notification(
                job,
                event_type="recovery_succeeded",
                body=(
                    f"Recovery agent successfully applied a targeted fix. "
                    f"Your job is returning to the pipeline.\n\n"
                    f"Action: {recovery_action}"
                ),
            )
        elif outcome == "needs_human":
            self._fire_notification(
                job,
                event_type="needs_human",
                body=report.summary,
            )

        return report

    def investigate_block(self, job: FactoryJob, conflicts: list[dict]) -> CleanupReport:
        """Mode 3 — called when a job transitions to BLOCKED.

        Analyzes why the job is blocked, examines the blocking job's state,
        evaluates whether the blocked job's plan is still viable, and recommends
        a resolution action (proceed / replan / cancel / wait).

        The report is stored in factory_cleanup_reports and its summary is
        returned for inclusion in the notification to the dev.
        """
        start = time.monotonic()

        # Collect conflicting file paths and blocking job ids
        conflict_files = [c["file_path"] for c in conflicts]
        blocking_job_ids = list({c["blocking_job_id"] for c in conflicts if c.get("blocking_job_id")})

        # Get blocking job details
        blocking_jobs_info: list[dict] = []
        for bjid in blocking_job_ids:
            bj = self.db.get_job(bjid)
            if bj:
                blocking_jobs_info.append({
                    "id": bjid,
                    "title": bj.title,
                    "status": bj.status.value,
                    "submitted_by": bj.submitted_by,
                    "error_count": bj.error_count,
                })

        # Classify: active vs completed blockers
        active_blockers = [
            b for b in blocking_jobs_info
            if b["status"] not in ("approved", "rejected", "deployed", "failed", "ready_for_approval")
        ]
        completed_blockers = [
            b for b in blocking_jobs_info
            if b["status"] in ("approved", "deployed", "ready_for_approval")
        ]

        # Determine recommendation
        if active_blockers:
            recommendation = "wait"
            rationale = (
                "Blocking job is still active. Coordinate with the other dev "
                "to determine the right resolution."
            )
        elif completed_blockers:
            recommendation = "replan"
            rationale = (
                "Blocking job has already completed. Since the codebase has "
                "changed, replanning is strongly recommended to ensure your "
                "plan still matches the current code."
            )
        else:
            recommendation = "proceed"
            rationale = "Blocking jobs are no longer active. Safe to proceed with original plan."

        # Build the summary
        summary_lines = [
            f"🔒 Job '{job.title}' is blocked on file lock conflicts.",
            "",
            f"**Conflicting files** ({len(conflict_files)}):",
        ]
        for f in conflict_files:
            summary_lines.append(f"  • {f}")

        if blocking_jobs_info:
            summary_lines.append("")
            summary_lines.append("**Blocking jobs:**")
            for bj in blocking_jobs_info:
                dev_tag = f" ({bj['submitted_by']})" if bj.get("submitted_by") else ""
                summary_lines.append(
                    f"  • {bj['title']}{dev_tag} — status: {bj['status']}"
                )

        summary_lines.extend([
            "",
            "**Recommendation:** " + recommendation,
            "",
            rationale,
            "",
            "**To resolve:**",
            "Ask your AI session: \"what's going on with my blocked job?\"",
            "Your AI has access to this investigation via DevBrain MCP tools",
            "(factory_status, factory_file_locks, deep_search) and can help",
            "you decide between proceed, replan, or cancel.",
            "",
            "Or use CLI:",
            f"  devbrain resolve {job.id[:8]} --proceed",
            f"  devbrain resolve {job.id[:8]} --replan",
            f"  devbrain resolve {job.id[:8]} --cancel",
        ])
        summary = "\n".join(summary_lines)

        elapsed = int(time.monotonic() - start)

        metadata = {
            "conflict_files": conflict_files,
            "blocking_job_ids": blocking_job_ids,
            "blocking_jobs": blocking_jobs_info,
            "blocking_dev_id": (
                blocking_jobs_info[0]["submitted_by"]
                if blocking_jobs_info and blocking_jobs_info[0].get("submitted_by")
                else None
            ),
            "recommendation": recommendation,
            "rationale": rationale,
        }

        # Persist
        self.db.store_cleanup_report(
            job_id=job.id,
            report_type="blocked_investigation",
            outcome="awaiting_resolution",
            summary=summary,
            recovery_diagnosis=rationale,
            recovery_action_taken=f"recommendation: {recommendation}",
            time_elapsed_seconds=elapsed,
            metadata=metadata,
        )

        return CleanupReport(
            job_id=job.id,
            report_type="blocked_investigation",
            outcome="awaiting_resolution",
            summary=summary,
            phases_traversed=[],
            artifacts_summary={},
            recovery_diagnosis=rationale,
            recovery_action_taken=f"recommendation: {recommendation}",
            time_elapsed_seconds=elapsed,
            metadata=metadata,
        )

    def _fire_notification(
        self,
        job: FactoryJob,
        event_type: str,
        body: str,
    ) -> None:
        """Helper to fire a notification through the router. Non-blocking."""
        try:
            if not job.submitted_by:
                return
            router = NotificationRouter(self.db)
            router.send(NotificationEvent(
                event_type=event_type,
                recipient_dev_id=job.submitted_by,
                title=self._notification_title(job, event_type),
                body=body,
                job_id=job.id,
            ))
        except Exception as e:
            logger.warning(
                "%s notification failed for job %s: %s (non-blocking)",
                event_type, job.id[:8], e,
            )

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

    def _attempt_targeted_fix(self, job: FactoryJob, diagnosis: dict) -> tuple[bool, str]:
        """Run a targeted fix via CLI executor.

        Returns (success, description) tuple.
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
                return True, f"Targeted fix applied via {cli_name}. stdout: {result.stdout[:500]}"
            else:
                return False, f"Fix attempt via {cli_name} failed (exit {result.exit_code}): {result.stderr[:500]}"
        except Exception as exc:
            logger.error("Targeted fix failed: %s", exc)
            return False, f"Fix attempt raised exception: {exc}"

    def _get_project_root(self, job: FactoryJob) -> str:
        """Derive the project working directory for a job.

        Lookup order:
          1. job.metadata['project_root'] — set by the orchestrator at submission
          2. config: factory.project_paths[<slug>] — per-project mapping in devbrain.yaml
          3. ~/<slug> — last-ditch fallback (logs a warning)
        """
        if "project_root" in job.metadata:
            return job.metadata["project_root"]
        configured = _project_path(job.project_slug)
        if configured:
            return configured
        from pathlib import Path as _Path
        fallback = str(_Path.home() / job.project_slug)
        logger.warning(
            "No project_root in metadata and no factory.project_paths['%s'] in config; "
            "falling back to %s",
            job.project_slug, fallback,
        )
        return fallback

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
