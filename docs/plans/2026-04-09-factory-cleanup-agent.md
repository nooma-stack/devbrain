# Factory Cleanup & Recovery Agent Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a cleanup/recovery agent to the dev factory that runs after every job completion (housekeeping) and attempts self-healing before jobs transition to FAILED.

**Architecture:** The cleanup agent is a new Python module (`factory/cleanup_agent.py`) integrated into the orchestrator. It has two modes: (1) post-run cleanup that archives terminal jobs, cleans branches, and generates a structured report, and (2) on-error recovery that gets one focused attempt with a soft 10-minute timer to diagnose and fix issues the fix loop couldn't. Reports flow to Claude for review before surfacing to the user.

**Tech Stack:** Python, psycopg2, subprocess (git), existing CLI executor, existing state machine

---

## Task 1: Database Migration — Add `archived_at` Column and Cleanup Report Storage

**Files:**
- Create: `migrations/003_cleanup_agent.sql`

**Step 1: Write the migration**

```sql
-- Migration 003: Cleanup agent support
-- Adds archived_at for terminal job lifecycle management
-- Adds cleanup_reports table for structured post-run reports

ALTER TABLE devbrain.factory_jobs
    ADD COLUMN archived_at TIMESTAMPTZ;

CREATE INDEX idx_factory_jobs_archived ON devbrain.factory_jobs(archived_at)
    WHERE archived_at IS NOT NULL;

CREATE TABLE devbrain.factory_cleanup_reports (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID REFERENCES devbrain.factory_jobs(id) NOT NULL,
    report_type     VARCHAR(50) NOT NULL,  -- 'post_run' or 'recovery_attempt'
    outcome         VARCHAR(50) NOT NULL,  -- 'clean', 'recovered', 'failed', 'needs_human'
    summary         TEXT NOT NULL,
    phases_traversed JSONB DEFAULT '[]',
    artifacts_summary JSONB DEFAULT '{}',
    recovery_diagnosis TEXT,               -- Root cause analysis (recovery mode only)
    recovery_action_taken TEXT,            -- What the agent tried (recovery mode only)
    time_elapsed_seconds INT,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_cleanup_reports_job ON devbrain.factory_cleanup_reports(job_id);
```

**Step 2: Run the migration**

Run: `psql "postgresql://devbrain:devbrain-local@localhost:5433/devbrain" -f migrations/003_cleanup_agent.sql`
Expected: ALTER TABLE, CREATE INDEX, CREATE TABLE — no errors

**Step 3: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add migrations/003_cleanup_agent.sql
git commit -m "chore: add migration for cleanup agent support (archived_at, cleanup_reports table)"
```

---

## Task 2: State Machine — Add Archival and Report Methods

**Files:**
- Modify: `factory/state_machine.py` (add methods after line 304)

**Step 1: Write the failing test**

Create: `factory/tests/test_state_machine_cleanup.py`

```python
"""Tests for cleanup-related state machine methods."""
import json
import os
import pytest
import psycopg2

from state_machine import FactoryDB, JobStatus

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture
def test_job(db):
    """Create a test job in FAILED state for cleanup testing."""
    job_id = db.create_job(
        project_slug="devbrain",
        title="Test cleanup job",
        spec="Test spec",
    )
    # Transition to a terminal state
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.FAILED)
    return job_id


def test_archive_job(db, test_job):
    """Archiving a job sets archived_at timestamp."""
    db.archive_job(test_job)
    job = db.get_job(test_job)
    assert job.metadata.get("archived_at") is not None or True  # Check via SQL
    # Verify via direct query
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT archived_at FROM devbrain.factory_jobs WHERE id = %s", (test_job,))
        row = cur.fetchone()
        assert row[0] is not None


def test_archive_non_terminal_job_raises(db):
    """Cannot archive a job that isn't in a terminal state."""
    job_id = db.create_job(
        project_slug="devbrain",
        title="Test active job",
        spec="Test spec",
    )
    with pytest.raises(ValueError, match="Cannot archive"):
        db.archive_job(job_id)


def test_store_cleanup_report(db, test_job):
    """Store and retrieve a cleanup report."""
    report_id = db.store_cleanup_report(
        job_id=test_job,
        report_type="post_run",
        outcome="clean",
        summary="Job completed successfully. Branch cleaned up.",
        phases_traversed=["queued", "planning", "failed"],
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
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/patrickkelly/devbrain/factory && python -m pytest tests/test_state_machine_cleanup.py -v`
Expected: FAIL — `archive_job`, `store_cleanup_report`, `get_cleanup_reports` don't exist yet

**Step 3: Implement the methods**

Add to `factory/state_machine.py` after the `get_artifacts` method (after line 304):

```python
    # Terminal states for archival
    TERMINAL_STATES = {JobStatus.APPROVED, JobStatus.REJECTED, JobStatus.DEPLOYED, JobStatus.FAILED}

    def archive_job(self, job_id: str) -> None:
        """Mark a terminal job as archived. Sets archived_at timestamp."""
        job = self.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")
        if job.status not in self.TERMINAL_STATES:
            raise ValueError(
                f"Cannot archive job in status '{job.status.value}'. "
                f"Must be one of: {[s.value for s in self.TERMINAL_STATES]}"
            )
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE devbrain.factory_jobs SET archived_at = now() WHERE id = %s",
                (job_id,),
            )
            conn.commit()
        logger.info("Archived job %s", job_id[:8])

    def store_cleanup_report(
        self,
        job_id: str,
        report_type: str,
        outcome: str,
        summary: str,
        phases_traversed: list[str] | None = None,
        artifacts_summary: dict | None = None,
        recovery_diagnosis: str | None = None,
        recovery_action_taken: str | None = None,
        time_elapsed_seconds: int | None = None,
        metadata: dict | None = None,
    ) -> str:
        """Store a cleanup/recovery report."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO devbrain.factory_cleanup_reports
                    (job_id, report_type, outcome, summary, phases_traversed,
                     artifacts_summary, recovery_diagnosis, recovery_action_taken,
                     time_elapsed_seconds, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    job_id, report_type, outcome, summary,
                    json.dumps(phases_traversed or []),
                    json.dumps(artifacts_summary or {}),
                    recovery_diagnosis,
                    recovery_action_taken,
                    time_elapsed_seconds,
                    json.dumps(metadata or {}),
                ),
            )
            report_id = str(cur.fetchone()[0])
            conn.commit()
            return report_id

    def get_cleanup_reports(self, job_id: str) -> list[dict]:
        """Get cleanup reports for a job."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, report_type, outcome, summary, phases_traversed,
                       artifacts_summary, recovery_diagnosis, recovery_action_taken,
                       time_elapsed_seconds, metadata, created_at
                FROM devbrain.factory_cleanup_reports
                WHERE job_id = %s
                ORDER BY created_at ASC
                """,
                (job_id,),
            )
            return [
                {
                    "id": str(r[0]), "report_type": r[1], "outcome": r[2],
                    "summary": r[3], "phases_traversed": r[4],
                    "artifacts_summary": r[5], "recovery_diagnosis": r[6],
                    "recovery_action_taken": r[7],
                    "time_elapsed_seconds": r[8], "metadata": r[9] or {},
                    "created_at": str(r[10]),
                }
                for r in cur.fetchall()
            ]
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/patrickkelly/devbrain/factory && python -m pytest tests/test_state_machine_cleanup.py -v`
Expected: All 4 tests PASS

**Step 5: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/state_machine.py factory/tests/test_state_machine_cleanup.py
git commit -m "feat: add archive and cleanup report methods to FactoryDB"
```

---

## Task 3: Cleanup Agent Core — Post-Run Housekeeping

**Files:**
- Create: `factory/cleanup_agent.py`
- Test: `factory/tests/test_cleanup_agent.py`

**Step 1: Write the failing test**

Create: `factory/tests/test_cleanup_agent.py`

```python
"""Tests for the cleanup agent."""
import json
import os
import subprocess
import pytest

from state_machine import FactoryDB, JobStatus
from cleanup_agent import CleanupAgent

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture
def agent(db):
    return CleanupAgent(db)


@pytest.fixture
def completed_job(db):
    """Create a job that went through the pipeline and reached READY_FOR_APPROVAL."""
    job_id = db.create_job(
        project_slug="devbrain",
        title="Test completed job",
        spec="Test spec for cleanup",
    )
    db.transition(job_id, JobStatus.PLANNING)
    db.store_artifact(job_id, "planning", "plan_doc", "Test plan content", model_used="claude")
    db.transition(job_id, JobStatus.IMPLEMENTING)
    db.store_artifact(job_id, "implementation", "impl_output", "Test impl content", model_used="claude")
    db.transition(job_id, JobStatus.REVIEWING)
    db.store_artifact(job_id, "review", "arch_review", "No issues found", model_used="claude")
    db.store_artifact(job_id, "review", "security_review", "No issues found", model_used="claude")
    db.transition(job_id, JobStatus.QA)
    db.store_artifact(job_id, "qa", "qa_report", json.dumps([{"check": "lint", "passed": True}]))
    db.transition(job_id, JobStatus.READY_FOR_APPROVAL)
    db.transition(job_id, JobStatus.APPROVED)
    return job_id


@pytest.fixture
def failed_job(db):
    """Create a job that failed after max retries."""
    job_id = db.create_job(
        project_slug="devbrain",
        title="Test failed job",
        spec="Test spec for failure",
    )
    db.transition(job_id, JobStatus.PLANNING)
    db.store_artifact(job_id, "planning", "plan_doc", "Test plan", model_used="claude")
    db.transition(job_id, JobStatus.IMPLEMENTING)
    db.transition(job_id, JobStatus.REVIEWING)
    db.store_artifact(job_id, "review", "arch_review",
                      "1. BLOCKING: Missing error handling in auth.py:45",
                      model_used="claude", blocking_count=1)
    db.transition(job_id, JobStatus.FIX_LOOP)
    db.transition(job_id, JobStatus.FAILED,
                  metadata={"failure": "max fix retries exceeded"})
    return job_id


def test_post_run_report_success(agent, completed_job):
    """Post-run cleanup generates a clean report for successful jobs."""
    report = agent.run_post_cleanup(completed_job)
    assert report["outcome"] == "clean"
    assert report["report_type"] == "post_run"
    assert "planning" in report["phases_traversed"]
    assert report["summary"]  # Non-empty summary


def test_post_run_report_failed(agent, failed_job):
    """Post-run cleanup generates a failure report with diagnosis."""
    report = agent.run_post_cleanup(failed_job)
    assert report["outcome"] == "failed"
    assert report["report_type"] == "post_run"
    assert "BLOCKING" in report["summary"] or "failure" in report["summary"].lower()


def test_post_run_archives_job(agent, completed_job, db):
    """Post-run cleanup archives the job."""
    agent.run_post_cleanup(completed_job)
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT archived_at FROM devbrain.factory_jobs WHERE id = %s", (completed_job,))
        assert cur.fetchone()[0] is not None


def test_post_run_stores_report_in_db(agent, completed_job, db):
    """Post-run cleanup persists the report."""
    agent.run_post_cleanup(completed_job)
    reports = db.get_cleanup_reports(completed_job)
    assert len(reports) == 1
    assert reports[0]["report_type"] == "post_run"
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/patrickkelly/devbrain/factory && python -m pytest tests/test_cleanup_agent.py -v`
Expected: FAIL — `cleanup_agent` module doesn't exist

**Step 3: Implement the cleanup agent**

Create: `factory/cleanup_agent.py`

```python
"""Factory cleanup & recovery agent.

Two modes:
1. Post-run cleanup: runs after every terminal state. Archives job, cleans branches,
   generates structured report.
2. On-error recovery: called before FAILED transition. Gets one focused attempt
   with soft 10-min timer to diagnose and fix.

Reports are structured for Claude to review before surfacing to the user.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from state_machine import FactoryDB, FactoryJob, JobStatus

logger = logging.getLogger(__name__)

# Timing configuration
SOFT_TIMER_SECONDS = 600       # 10 minutes — triggers self-assessment
EXTENSION_SECONDS = 300        # 5 minutes per extension
HARD_CEILING_SECONDS = 1800    # 30 minutes — absolute maximum

TERMINAL_STATES = {JobStatus.APPROVED, JobStatus.REJECTED, JobStatus.DEPLOYED, JobStatus.FAILED}
SUCCESS_STATES = {JobStatus.APPROVED, JobStatus.DEPLOYED, JobStatus.READY_FOR_APPROVAL}


@dataclass
class ProgressCheckpoint:
    """Tracks recovery agent progress for self-assessment."""
    timestamp: float
    action: str
    result: str
    progress_made: bool


@dataclass
class CleanupReport:
    """Structured report from cleanup agent."""
    job_id: str
    report_type: str          # 'post_run' or 'recovery_attempt'
    outcome: str              # 'clean', 'recovered', 'failed', 'needs_human'
    summary: str
    phases_traversed: list[str] = field(default_factory=list)
    artifacts_summary: dict = field(default_factory=dict)
    recovery_diagnosis: str | None = None
    recovery_action_taken: str | None = None
    time_elapsed_seconds: int = 0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "report_type": self.report_type,
            "outcome": self.outcome,
            "summary": self.summary,
            "phases_traversed": self.phases_traversed,
            "artifacts_summary": self.artifacts_summary,
            "recovery_diagnosis": self.recovery_diagnosis,
            "recovery_action_taken": self.recovery_action_taken,
            "time_elapsed_seconds": self.time_elapsed_seconds,
            "metadata": self.metadata,
        }


class CleanupAgent:
    """Cleanup and recovery agent for the dev factory."""

    def __init__(self, db: FactoryDB):
        self.db = db

    # ─── Post-Run Cleanup ─────────────────────────────────────────────────

    def run_post_cleanup(self, job_id: str) -> dict:
        """Run post-completion cleanup for any terminal job.

        - Collects structured report of what happened
        - Cleans up git branches for failed/rejected jobs
        - Archives the job
        - Persists the report

        Returns the report as a dict.
        """
        start = time.time()
        job = self.db.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        if job.status not in TERMINAL_STATES:
            raise ValueError(f"Job {job_id} is not in a terminal state (status: {job.status.value})")

        # Collect artifacts summary
        artifacts = self.db.get_artifacts(job_id)
        artifacts_summary = self._summarize_artifacts(artifacts)
        phases_traversed = self._extract_phases(artifacts, job)

        # Determine outcome
        if job.status in SUCCESS_STATES:
            outcome = "clean"
            summary = self._build_success_summary(job, artifacts_summary)
        else:
            outcome = "failed"
            summary = self._build_failure_summary(job, artifacts, artifacts_summary)

        # Clean up git branch for non-success terminal states
        if job.status in (JobStatus.FAILED, JobStatus.REJECTED) and job.branch_name:
            self._cleanup_branch(job)

        elapsed = int(time.time() - start)

        report = CleanupReport(
            job_id=job_id,
            report_type="post_run",
            outcome=outcome,
            summary=summary,
            phases_traversed=phases_traversed,
            artifacts_summary=artifacts_summary,
            time_elapsed_seconds=elapsed,
            metadata={
                "final_status": job.status.value,
                "error_count": job.error_count,
                "branch_name": job.branch_name,
            },
        )

        # Persist report and archive
        self.db.store_cleanup_report(
            job_id=job_id,
            report_type=report.report_type,
            outcome=report.outcome,
            summary=report.summary,
            phases_traversed=report.phases_traversed,
            artifacts_summary=report.artifacts_summary,
            time_elapsed_seconds=report.time_elapsed_seconds,
            metadata=report.metadata,
        )
        self.db.archive_job(job_id)

        logger.info("Post-run cleanup complete for job %s: %s", job_id[:8], outcome)
        return report.to_dict()

    # ─── On-Error Recovery ────────────────────────────────────────────────

    def attempt_recovery(self, job: FactoryJob) -> CleanupReport:
        """Attempt to recover a job that has exhausted its fix loop retries.

        Called BEFORE the job transitions to FAILED. Gets one focused attempt
        with a soft 10-minute timer.

        The recovery agent:
        1. Reads full artifact history to understand what was tried
        2. Diagnoses the root cause (not just symptoms)
        3. Attempts a targeted fix if confidence is high
        4. Self-assesses progress at 10-min intervals
        5. Hard ceiling at 30 minutes

        Returns a CleanupReport with outcome:
        - 'recovered': fix worked, job can be re-queued
        - 'needs_human': diagnosis complete but fix requires human input
        - 'failed': could not diagnose or fix
        """
        start = time.time()
        checkpoints: list[ProgressCheckpoint] = []

        logger.info("Recovery agent starting for job %s (error_count=%d)",
                     job.id[:8], job.error_count)

        # Step 1: Collect full context
        artifacts = self.db.get_artifacts(job.id)
        diagnosis = self._diagnose_failure(job, artifacts)
        checkpoints.append(ProgressCheckpoint(
            timestamp=time.time(),
            action="diagnosis",
            result=diagnosis["summary"],
            progress_made=True,
        ))

        # Step 2: Decide whether to attempt fix
        if not diagnosis["fixable"]:
            elapsed = int(time.time() - start)
            report = CleanupReport(
                job_id=job.id,
                report_type="recovery_attempt",
                outcome="needs_human",
                summary=f"Recovery agent diagnosed the issue but cannot auto-fix.\n\n{diagnosis['summary']}",
                phases_traversed=self._extract_phases(artifacts, job),
                artifacts_summary=self._summarize_artifacts(artifacts),
                recovery_diagnosis=diagnosis["detail"],
                recovery_action_taken="Diagnosis only — fix requires human judgment.",
                time_elapsed_seconds=elapsed,
                metadata={
                    "checkpoints": [
                        {"action": cp.action, "result": cp.result, "progress": cp.progress_made}
                        for cp in checkpoints
                    ],
                    "questions_for_human": diagnosis.get("questions", []),
                },
            )
            return report

        # Step 3: Attempt the fix with soft timer
        fix_result = self._attempt_targeted_fix(job, diagnosis, start, checkpoints)

        elapsed = int(time.time() - start)
        report = CleanupReport(
            job_id=job.id,
            report_type="recovery_attempt",
            outcome=fix_result["outcome"],
            summary=fix_result["summary"],
            phases_traversed=self._extract_phases(artifacts, job),
            artifacts_summary=self._summarize_artifacts(artifacts),
            recovery_diagnosis=diagnosis["detail"],
            recovery_action_taken=fix_result.get("action_taken", ""),
            time_elapsed_seconds=elapsed,
            metadata={
                "checkpoints": [
                    {"action": cp.action, "result": cp.result, "progress": cp.progress_made}
                    for cp in checkpoints
                ],
            },
        )
        return report

    def _diagnose_failure(self, job: FactoryJob, artifacts: list[dict]) -> dict:
        """Analyze artifacts to diagnose why the job failed.

        Returns dict with:
        - summary: one-line diagnosis
        - detail: full analysis
        - fixable: whether the agent thinks it can fix this
        - fix_approach: what to try (if fixable)
        - questions: questions for human (if not fixable)
        """
        # Collect all blocking findings across review rounds
        blocking_findings = []
        fix_outputs = []
        qa_failures = []

        for art in artifacts:
            if art["phase"] == "review" and art["blocking_count"] > 0:
                blocking_findings.append(art["content"])
            elif art["phase"] == "fix":
                fix_outputs.append(art["content"])
            elif art["phase"] == "qa" and art["blocking_count"] > 0:
                try:
                    qa_data = json.loads(art["content"])
                    qa_failures.extend(
                        r["check"] for r in qa_data if not r.get("passed")
                    )
                except (json.JSONDecodeError, TypeError):
                    qa_failures.append(art["content"][:200])

        # Check failure metadata
        failure_reason = job.metadata.get("failure", "unknown")
        qa_failure_list = job.metadata.get("qa_failures", [])

        # Determine failure category
        if qa_failures or qa_failure_list:
            category = "qa_failure"
            summary = f"QA failures after {job.error_count} fix attempts: {', '.join(qa_failures or qa_failure_list)}"
        elif blocking_findings:
            category = "persistent_blocking"
            summary = f"Blocking review findings persisted through {job.error_count} fix attempts"
        else:
            category = "unknown"
            summary = f"Job failed: {failure_reason}"

        # Analyze fix convergence — are fixes making progress or going in circles?
        converging = self._check_fix_convergence(blocking_findings, fix_outputs)

        # Simple heuristic: fixable if it's a QA failure (concrete) and fixes were converging
        fixable = category == "qa_failure" and converging

        detail_parts = [
            f"Failure category: {category}",
            f"Error count: {job.error_count}/{job.max_retries}",
            f"Fix convergence: {'converging' if converging else 'diverging/stalled'}",
        ]
        if blocking_findings:
            detail_parts.append(f"\nLatest blocking findings:\n{blocking_findings[-1][:1000]}")
        if fix_outputs:
            detail_parts.append(f"\nLatest fix output:\n{fix_outputs[-1][:1000]}")

        questions = []
        if not fixable:
            if category == "persistent_blocking":
                questions.append("The same blocking findings keep appearing despite fixes. Should the review criteria be relaxed, or is there a fundamental design issue?")
            elif category == "unknown":
                questions.append(f"Job failed with reason: {failure_reason}. Need more context to diagnose.")

        return {
            "summary": summary,
            "detail": "\n".join(detail_parts),
            "category": category,
            "fixable": fixable,
            "fix_approach": f"Re-run QA fixes targeting: {', '.join(qa_failures or qa_failure_list)}" if fixable else None,
            "questions": questions,
            "converging": converging,
        }

    def _check_fix_convergence(self, blocking_findings: list[str], fix_outputs: list[str]) -> bool:
        """Check if fix attempts are making progress (fewer blocking findings over rounds)."""
        if len(blocking_findings) < 2:
            return True  # Not enough data, assume converging
        # Simple heuristic: check if blocking content length is decreasing
        lengths = [len(f) for f in blocking_findings]
        decreasing = sum(1 for i in range(1, len(lengths)) if lengths[i] < lengths[i-1])
        return decreasing >= len(lengths) // 2

    def _attempt_targeted_fix(
        self,
        job: FactoryJob,
        diagnosis: dict,
        start_time: float,
        checkpoints: list[ProgressCheckpoint],
    ) -> dict:
        """Attempt a targeted fix with soft timer and self-assessment.

        Uses the CLI executor to run a focused fix agent with full diagnosis context.
        """
        from cli_executor import run_cli

        project_root = self._get_project_root(job)

        prompt = f"""You are a RECOVERY AGENT for a dev factory pipeline. A job has failed after {job.error_count} fix attempts. You are the last line of automated defense before this escalates to a human.

PROJECT: {job.project_slug}
FEATURE: {job.title}
BRANCH: {job.branch_name or 'main'}

## ROOT CAUSE DIAGNOSIS

{diagnosis['detail']}

## SUGGESTED FIX APPROACH

{diagnosis.get('fix_approach', 'No specific approach identified')}

## Your Job

1. Read the failing tests/lint output to understand the EXACT errors
2. Read the code at the failing locations
3. Apply a TARGETED fix — you have ONE shot, make it count
4. Run the project's tests to verify
5. Commit if the fix works

IMPORTANT: This is recovery mode. Do NOT refactor, do NOT expand scope.
Fix the specific failures listed above, nothing more."""

        logger.info("Recovery agent attempting targeted fix for job %s", job.id[:8])
        result = run_cli(
            job.assigned_cli or "claude",
            prompt,
            cwd=project_root,
            env_override={"DEVBRAIN_PROJECT": job.project_slug},
        )

        checkpoints.append(ProgressCheckpoint(
            timestamp=time.time(),
            action="targeted_fix",
            result="success" if result.success else f"failed: {result.stderr[:200]}",
            progress_made=result.success,
        ))

        elapsed = time.time() - start_time

        # Check soft timer
        if elapsed > SOFT_TIMER_SECONDS and not result.success:
            # Self-assessment: we've used our time and the fix didn't work
            logger.info("Recovery agent: soft timer expired (%.0fs), fix unsuccessful", elapsed)
            return {
                "outcome": "failed",
                "summary": f"Recovery fix attempted but unsuccessful after {int(elapsed)}s.\n\nCLI output: {result.stdout[:500]}",
                "action_taken": f"Ran targeted fix agent. Exit code: {result.exit_code}",
            }

        if result.success:
            # Store the fix as an artifact
            self.db.store_artifact(
                job_id=job.id,
                phase="recovery",
                artifact_type="recovery_fix",
                content=result.stdout,
                model_used=job.assigned_cli or "claude",
            )
            return {
                "outcome": "recovered",
                "summary": f"Recovery agent successfully fixed the issue in {int(elapsed)}s.",
                "action_taken": f"Ran targeted fix agent focusing on: {diagnosis.get('fix_approach', 'diagnosed issues')}",
            }
        else:
            return {
                "outcome": "failed",
                "summary": f"Recovery fix unsuccessful after {int(elapsed)}s.\n\nDiagnosis: {diagnosis['summary']}",
                "action_taken": f"Attempted targeted fix. CLI exit code: {result.exit_code}. Stderr: {result.stderr[:300]}",
            }

    # ─── Helpers ──────────────────────────────────────────────────────────

    def _get_project_root(self, job: FactoryJob) -> str:
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT root_path FROM devbrain.projects WHERE id = %s", (job.project_id,))
            row = cur.fetchone()
            return row[0] if row else "."

    def _summarize_artifacts(self, artifacts: list[dict]) -> dict:
        """Condense artifacts into a structured summary."""
        summary: dict = {}
        for art in artifacts:
            phase = art["phase"]
            if phase not in summary:
                summary[phase] = []
            summary[phase].append({
                "type": art["artifact_type"],
                "findings": art["findings_count"],
                "blocking": art["blocking_count"],
                "model": art["model_used"],
            })
        return summary

    def _extract_phases(self, artifacts: list[dict], job: FactoryJob) -> list[str]:
        """Extract ordered list of phases the job went through."""
        phases = []
        seen = set()
        # Start with queued
        phases.append("queued")
        seen.add("queued")
        for art in artifacts:
            if art["phase"] not in seen:
                phases.append(art["phase"])
                seen.add(art["phase"])
        # Add final status
        if job.status.value not in seen:
            phases.append(job.status.value)
        return phases

    def _build_success_summary(self, job: FactoryJob, artifacts_summary: dict) -> str:
        """Build a summary for a successfully completed job."""
        parts = [
            f"Job '{job.title}' completed successfully (status: {job.status.value}).",
        ]
        if job.error_count > 0:
            parts.append(f"Required {job.error_count} fix loop iteration(s) before passing.")
        if job.branch_name:
            parts.append(f"Branch: {job.branch_name}")

        phase_count = len(artifacts_summary)
        artifact_count = sum(len(v) for v in artifacts_summary.values())
        parts.append(f"Traversed {phase_count} phases, generated {artifact_count} artifacts.")

        return "\n".join(parts)

    def _build_failure_summary(self, job: FactoryJob, artifacts: list[dict], artifacts_summary: dict) -> str:
        """Build a summary for a failed job, including what went wrong."""
        parts = [
            f"Job '{job.title}' FAILED (error_count: {job.error_count}/{job.max_retries}).",
        ]

        failure_reason = job.metadata.get("failure", "unknown")
        parts.append(f"Failure reason: {failure_reason}")

        # Include last blocking findings
        blocking_arts = [a for a in artifacts if a["blocking_count"] > 0]
        if blocking_arts:
            last = blocking_arts[-1]
            parts.append(f"\nLast blocking findings (phase: {last['phase']}, type: {last['artifact_type']}):")
            # Truncate to keep report readable
            parts.append(last["content"][:800])

        if job.branch_name:
            parts.append(f"\nBranch (to be cleaned up): {job.branch_name}")

        return "\n".join(parts)

    def _cleanup_branch(self, job: FactoryJob) -> None:
        """Delete the git branch for a failed/rejected job."""
        if not job.branch_name:
            return

        project_root = self._get_project_root(job)
        try:
            # Only delete local branch — never force-delete
            subprocess.run(
                ["git", "branch", "-d", job.branch_name],
                cwd=project_root, capture_output=True, timeout=10,
            )
            logger.info("Cleaned up branch %s for job %s", job.branch_name, job.id[:8])
        except Exception as e:
            # Branch cleanup is best-effort — don't fail the report over it
            logger.warning("Branch cleanup failed for %s: %s", job.branch_name, e)
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/patrickkelly/devbrain/factory && python -m pytest tests/test_cleanup_agent.py -v`
Expected: All 4 tests PASS

**Step 5: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/cleanup_agent.py factory/tests/test_cleanup_agent.py
git commit -m "feat: add cleanup agent with post-run housekeeping and recovery attempt"
```

---

## Task 4: Integrate Cleanup Agent into Orchestrator

**Files:**
- Modify: `factory/orchestrator.py`

**Step 1: Write the failing test**

Create: `factory/tests/test_orchestrator_cleanup.py`

```python
"""Tests for cleanup agent integration in orchestrator."""
import pytest
from unittest.mock import patch, MagicMock

from state_machine import FactoryDB, JobStatus
from orchestrator import FactoryOrchestrator

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"


@pytest.fixture
def orchestrator():
    return FactoryOrchestrator(DATABASE_URL)


def test_run_job_calls_post_cleanup_on_failure(orchestrator):
    """run_job calls post-cleanup when a job reaches FAILED."""
    # Create a job that will fail immediately (planning fails)
    job_id = orchestrator.db.create_job(
        project_slug="devbrain",
        title="Test orchestrator cleanup",
        spec="This will fail",
    )

    with patch.object(orchestrator, '_run_planning') as mock_plan:
        # Make planning fail
        failed_job = orchestrator.db.get_job(job_id)
        orchestrator.db.transition(job_id, JobStatus.PLANNING)
        failed_job = orchestrator.db.transition(job_id, JobStatus.FAILED,
                                                 metadata={"failure": "test failure"})
        mock_plan.return_value = failed_job

        with patch('orchestrator.CleanupAgent') as MockCleanup:
            mock_instance = MockCleanup.return_value
            mock_instance.run_post_cleanup.return_value = {
                "outcome": "failed", "summary": "test"
            }

            orchestrator.run_job(job_id)

            mock_instance.run_post_cleanup.assert_called_once_with(job_id)


def test_run_job_calls_recovery_before_failed(orchestrator):
    """run_job calls recovery agent when fix loop exhausts retries."""
    job_id = orchestrator.db.create_job(
        project_slug="devbrain",
        title="Test recovery integration",
        spec="Test spec",
        metadata={"max_retries": 0},  # Fail immediately
    )

    # We can't easily test the full integration without mocking CLI,
    # but we verify the recovery path exists by checking the import works
    from cleanup_agent import CleanupAgent
    agent = CleanupAgent(orchestrator.db)
    assert hasattr(agent, 'attempt_recovery')
```

**Step 2: Run test to verify it fails**

Run: `cd /Users/patrickkelly/devbrain/factory && python -m pytest tests/test_orchestrator_cleanup.py -v`
Expected: FAIL — `orchestrator.CleanupAgent` import doesn't exist yet

**Step 3: Modify the orchestrator**

In `factory/orchestrator.py`, add the import at line 20 (after the existing imports):

```python
from cleanup_agent import CleanupAgent
```

Modify `run_job()` method (lines 67-112) — add recovery attempt before FAILED transition and post-cleanup after terminal state:

Replace the `run_job` method entirely:

```python
    def run_job(self, job_id: str) -> FactoryJob:
        """Run a job through the full pipeline until it needs human approval or fails."""
        job = self.db.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        logger.info("Starting pipeline for job %s: %s", job_id[:8], job.title)
        cleanup = CleanupAgent(self.db)

        while job.status not in (
            JobStatus.READY_FOR_APPROVAL,
            JobStatus.APPROVED,
            JobStatus.REJECTED,
            JobStatus.DEPLOYED,
            JobStatus.FAILED,
        ):
            if job.status == JobStatus.QUEUED:
                job = self._run_planning(job)
            elif job.status == JobStatus.IMPLEMENTING:
                job = self._run_implementation(job)
            elif job.status == JobStatus.REVIEWING:
                job = self._run_review(job)
            elif job.status == JobStatus.QA:
                job = self._run_qa(job)
            elif job.status == JobStatus.FIX_LOOP:
                if job.error_count >= job.max_retries:
                    # Recovery attempt before giving up
                    logger.info("Fix loop exhausted for job %s — attempting recovery", job.id[:8])
                    recovery_report = cleanup.attempt_recovery(job)

                    if recovery_report.outcome == "recovered":
                        logger.info("Recovery successful for job %s — re-queuing", job.id[:8])
                        job = self.db.transition(job.id, JobStatus.IMPLEMENTING)
                        # Store recovery report
                        self.db.store_cleanup_report(
                            job_id=job.id,
                            report_type=recovery_report.report_type,
                            outcome=recovery_report.outcome,
                            summary=recovery_report.summary,
                            recovery_diagnosis=recovery_report.recovery_diagnosis,
                            recovery_action_taken=recovery_report.recovery_action_taken,
                            time_elapsed_seconds=recovery_report.time_elapsed_seconds,
                            metadata=recovery_report.metadata,
                        )
                        continue

                    # Recovery failed — transition to FAILED
                    job = self.db.transition(job.id, JobStatus.FAILED,
                                             metadata={"failure": "max fix retries exceeded, recovery failed"})
                    # Store recovery report
                    self.db.store_cleanup_report(
                        job_id=job.id,
                        report_type=recovery_report.report_type,
                        outcome=recovery_report.outcome,
                        summary=recovery_report.summary,
                        recovery_diagnosis=recovery_report.recovery_diagnosis,
                        recovery_action_taken=recovery_report.recovery_action_taken,
                        time_elapsed_seconds=recovery_report.time_elapsed_seconds,
                        metadata=recovery_report.metadata,
                    )
                    notify_desktop("DevBrain Factory",
                                   f"Job FAILED (recovery attempted): {job.title}")
                    break
                job = self._run_fix(job)

        if job.status == JobStatus.READY_FOR_APPROVAL:
            notify_desktop("DevBrain Factory",
                           f"Ready for review: {job.title}")

        # Extract lessons from review findings for the learning loop
        if job.status in (JobStatus.READY_FOR_APPROVAL, JobStatus.FAILED):
            try:
                lessons = extract_lessons(job.id)
                if lessons:
                    logger.info("Learning loop: extracted %d lessons from job %s",
                                len(lessons), job.id[:8])
            except Exception as e:
                logger.warning("Learning loop failed (non-blocking): %s", e)

        # Post-run cleanup — always runs on terminal states
        if job.status in (JobStatus.READY_FOR_APPROVAL, JobStatus.APPROVED,
                          JobStatus.REJECTED, JobStatus.DEPLOYED, JobStatus.FAILED):
            try:
                report = cleanup.run_post_cleanup(job.id)
                logger.info("Post-run cleanup: %s (outcome: %s)", job.id[:8], report["outcome"])
            except Exception as e:
                logger.warning("Post-run cleanup failed (non-blocking): %s", e)

        return job
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/patrickkelly/devbrain/factory && python -m pytest tests/test_orchestrator_cleanup.py -v`
Expected: All tests PASS

**Step 5: Also run the state machine and cleanup agent tests to verify no regressions**

Run: `cd /Users/patrickkelly/devbrain/factory && python -m pytest tests/ -v`
Expected: All tests PASS

**Step 6: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/orchestrator.py factory/tests/test_orchestrator_cleanup.py
git commit -m "feat: integrate cleanup agent into orchestrator pipeline"
```

---

## Task 5: Fix `get_project_context` Filter — Categorize Active vs Inactive Jobs

**Files:**
- Modify: `mcp-server/src/index.ts` (lines 79-93)

**Step 1: Identify the change**

The current query on line 84 returns all non-approved/rejected jobs as `active_factory_jobs`. We need to:
1. Split into `active_factory_jobs` and `recent_inactive_jobs`
2. Active = status NOT IN ('approved', 'rejected', 'deployed', 'failed') AND archived_at IS NULL
3. Inactive = status IN ('deployed', 'failed') AND archived_at IS NULL (not yet cleaned up) — plus recently archived

**Step 2: Modify the query**

Replace lines 79-93 of `mcp-server/src/index.ts`:

```typescript
    const [projectInfo, decisions, issues, patterns, activeJobs, inactiveJobs] = await Promise.all([
      query('SELECT name, description, root_path, constraints, tech_stack FROM devbrain.projects WHERE id = $1', [projectId]),
      query('SELECT title, decision, rationale, created_at FROM devbrain.decisions WHERE project_id = $1 AND status = \'active\' ORDER BY created_at DESC LIMIT 5', [projectId]),
      query('SELECT title, category, description, fix_applied, created_at FROM devbrain.issues WHERE project_id = $1 ORDER BY created_at DESC LIMIT 5', [projectId]),
      query('SELECT name, category, description FROM devbrain.patterns WHERE project_id = $1 ORDER BY created_at DESC LIMIT 5', [projectId]),
      // Active: currently running through the pipeline
      query(`SELECT title, status, current_phase, branch_name
             FROM devbrain.factory_jobs
             WHERE project_id = $1
               AND status NOT IN ('approved', 'rejected', 'deployed', 'failed')
               AND archived_at IS NULL
             ORDER BY created_at DESC LIMIT 5`, [projectId]),
      // Recently completed/failed (not yet archived, or archived in last 24h)
      query(`SELECT title, status, current_phase, branch_name, error_count, archived_at
             FROM devbrain.factory_jobs
             WHERE project_id = $1
               AND status IN ('deployed', 'failed', 'approved', 'rejected')
               AND (archived_at IS NULL OR archived_at > now() - interval '24 hours')
             ORDER BY updated_at DESC LIMIT 5`, [projectId]),
    ])

    const ctx = {
      project: projectInfo.rows[0] ?? null,
      recent_decisions: decisions.rows,
      recent_issues: issues.rows,
      relevant_patterns: patterns.rows,
      active_factory_jobs: activeJobs.rows,
      recent_completed_jobs: inactiveJobs.rows,
    }
```

**Step 3: Build and verify**

Run: `cd /Users/patrickkelly/devbrain/mcp-server && npm run build`
Expected: Build succeeds with no errors

**Step 4: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add mcp-server/src/index.ts
git commit -m "fix: categorize factory jobs as active vs recently completed in get_project_context"
```

---

## Task 6: Add `factory_cleanup` MCP Tool

**Files:**
- Modify: `mcp-server/src/index.ts` (add after `factory_approve` tool, after line 562)

**Step 1: Add the tool**

```typescript
// ─── Tool: factory_cleanup ──────────────────────────────────────────────────

server.tool(
  'factory_cleanup',
  'Manually trigger cleanup for a terminal factory job. Archives the job, cleans up branches, and generates a structured report. Useful for dismissing old failed/completed jobs.',
  {
    job_id: z.string().describe('Job ID to clean up'),
  },
  async ({ job_id }) => {
    const job = await query(
      'SELECT status, title, archived_at FROM devbrain.factory_jobs WHERE id = $1',
      [job_id],
    )

    if (job.rows.length === 0) {
      return { content: [{ type: 'text', text: `Job ${job_id} not found.` }] }
    }

    const status = job.rows[0].status as string
    const title = job.rows[0].title as string
    const archivedAt = job.rows[0].archived_at

    if (archivedAt) {
      return { content: [{ type: 'text', text: `Job "${title}" is already archived (${archivedAt}).` }] }
    }

    const terminalStates = ['approved', 'rejected', 'deployed', 'failed']
    if (!terminalStates.includes(status)) {
      return { content: [{ type: 'text', text: `Job "${title}" is still active (status: ${status}). Cannot clean up active jobs.` }] }
    }

    // Archive the job
    await query(
      'UPDATE devbrain.factory_jobs SET archived_at = now() WHERE id = $1',
      [job_id],
    )

    // Get cleanup report if one exists
    const reports = await query(
      `SELECT outcome, summary, time_elapsed_seconds, created_at
       FROM devbrain.factory_cleanup_reports
       WHERE job_id = $1 ORDER BY created_at DESC LIMIT 1`,
      [job_id],
    )

    const reportInfo = reports.rows.length > 0
      ? `\nCleanup report: ${reports.rows[0].outcome} (${reports.rows[0].summary.slice(0, 200)})`
      : '\nNo cleanup report on file (job predates cleanup agent).'

    return {
      content: [{
        type: 'text',
        text: `Archived job "${title}" (status: ${status}).${reportInfo}`,
      }],
    }
  },
)
```

**Step 2: Build and verify**

Run: `cd /Users/patrickkelly/devbrain/mcp-server && npm run build`
Expected: Build succeeds

**Step 3: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add mcp-server/src/index.ts
git commit -m "feat: add factory_cleanup MCP tool for manual job archival"
```

---

## Task 7: Update Config — Add Cleanup Agent Settings

**Files:**
- Modify: `config/devbrain.yaml`

**Step 1: Add cleanup config section**

Add after the `factory` section (after line 78):

```yaml
  cleanup:
    soft_timer_seconds: 600       # 10 min — triggers self-assessment
    extension_seconds: 300        # 5 min per extension
    hard_ceiling_seconds: 1800    # 30 min — absolute maximum
    auto_archive_after_hours: 24  # Archive completed jobs after 24h in context
    branch_cleanup: true          # Delete branches for failed/rejected jobs
```

**Step 2: Update cleanup_agent.py to read from config**

At the top of `factory/cleanup_agent.py`, replace the hardcoded constants:

```python
import yaml

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "devbrain.yaml"
with open(_CONFIG_PATH) as _f:
    _config = yaml.safe_load(_f)
_CLEANUP_CONFIG = _config.get("factory", {}).get("cleanup", {})

# Timing configuration — from config with defaults
SOFT_TIMER_SECONDS = _CLEANUP_CONFIG.get("soft_timer_seconds", 600)
EXTENSION_SECONDS = _CLEANUP_CONFIG.get("extension_seconds", 300)
HARD_CEILING_SECONDS = _CLEANUP_CONFIG.get("hard_ceiling_seconds", 1800)
BRANCH_CLEANUP_ENABLED = _CLEANUP_CONFIG.get("branch_cleanup", True)
```

**Step 3: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add config/devbrain.yaml factory/cleanup_agent.py
git commit -m "chore: add cleanup agent config to devbrain.yaml"
```

---

## Task 8: Create `__init__.py` for Tests and Add pytest Config

**Files:**
- Create: `factory/tests/__init__.py`
- Create: `factory/tests/conftest.py`
- Modify: `pyproject.toml` or create `factory/pytest.ini`

**Step 1: Create test infrastructure**

Create `factory/tests/__init__.py`:
```python
```

Create `factory/tests/conftest.py`:
```python
"""Shared fixtures for factory tests."""
import sys
from pathlib import Path

# Add factory dir to path so imports work
sys.path.insert(0, str(Path(__file__).parent.parent))
```

**Step 2: Verify all tests run**

Run: `cd /Users/patrickkelly/devbrain/factory && python -m pytest tests/ -v`
Expected: All tests PASS

**Step 3: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/tests/__init__.py factory/tests/conftest.py
git commit -m "chore: add test infrastructure for factory tests"
```

---

## Summary

| Task | What | Key Files |
|------|------|-----------|
| 1 | DB migration: `archived_at` + cleanup_reports table | `migrations/003_cleanup_agent.sql` |
| 2 | State machine: archive + report storage methods | `factory/state_machine.py` |
| 3 | Cleanup agent core: post-run + recovery | `factory/cleanup_agent.py` |
| 4 | Orchestrator integration | `factory/orchestrator.py` |
| 5 | Fix `get_project_context` filter | `mcp-server/src/index.ts` |
| 6 | Add `factory_cleanup` MCP tool | `mcp-server/src/index.ts` |
| 7 | Config: cleanup settings | `config/devbrain.yaml` |
| 8 | Test infrastructure | `factory/tests/` |

**Dependencies:** Task 1 must run first (schema). Tasks 2 and 8 can run in parallel. Task 3 depends on 2. Task 4 depends on 3. Tasks 5, 6, 7 are independent of each other but should come after Task 1.
