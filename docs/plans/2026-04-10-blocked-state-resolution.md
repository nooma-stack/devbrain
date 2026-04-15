# Blocked State + Dev-Driven Resolution Implementation Plan

> **Historical planning document.** Absolute paths and test commands in
> this doc reflect the dev environment at authorship time. For current
> install and test procedures see [INSTALL.md](../../INSTALL.md).
>
> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the auto-polling WAITING state with a BLOCKED state that halts the job indefinitely, lets the cleanup agent investigate the block and file a findings report, notifies devs with that report, and provides MCP tools / CLI for devs to resolve (proceed/replan/cancel) via their AI session or terminal.

**Architecture:** When a job hits a file lock conflict, it transitions to BLOCKED and the cleanup agent immediately runs a new `investigate_block` analysis that diagnoses the conflict, evaluates whether the blocked job's plan is still viable given the blocking job's changes, and recommends an action. This investigation is stored as a cleanup report. The notification delivered to both devs includes the investigation summary, so the dev's AI session (Claude/Codex/Gemini in tmux) can immediately understand the situation without re-investigating. The factory process exits after recording the block — no polling. When the dev resolves the block via the `devbrain_resolve_blocked` MCP tool (or CLI fallback), a new detached factory process is spawned to execute the resolution.

**Tech Stack:** Python (factory, cleanup agent, CLI), psycopg2, TypeScript (MCP tool), existing notification system

---

## Task 1: DB Migration — WAITING→BLOCKED, resolution column, report type

**Files:**
- Create: `migrations/006_blocked_state.sql`

**Step 1: Write the migration**

```sql
-- Migration 006: Replace WAITING state with BLOCKED; add dev-driven resolution.

-- Add the resolution column for dev-driven unblocking
ALTER TABLE devbrain.factory_jobs
    ADD COLUMN blocked_resolution VARCHAR(20);
-- Values: 'proceed' | 'replan' | 'cancel' | NULL

-- Migrate any existing WAITING jobs to BLOCKED (should be none in practice,
-- but be safe for any dev data)
UPDATE devbrain.factory_jobs
    SET status = 'blocked', current_phase = 'blocked'
    WHERE status = 'waiting';

-- Index on blocked_resolution for quick lookup of jobs with pending resolutions
CREATE INDEX idx_factory_jobs_blocked_resolution
    ON devbrain.factory_jobs(blocked_resolution)
    WHERE blocked_resolution IS NOT NULL;
```

**Step 2: Run the migration**

Run: `psql "postgresql://devbrain:devbrain-local@localhost:5433/devbrain" -f migrations/006_blocked_state.sql`
Expected: ALTER TABLE, UPDATE 0, CREATE INDEX — no errors

**Step 3: Verify**

Run: `psql "postgresql://devbrain:devbrain-local@localhost:5433/devbrain" -c "\d devbrain.factory_jobs" | grep -i blocked`
Expected: Shows `blocked_resolution` column and its index

**Step 4: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add migrations/006_blocked_state.sql
git commit -m "chore: add blocked_resolution column and migrate waiting→blocked"
```

---

## Task 2: State Machine — Rename WAITING→BLOCKED with new transitions

**Files:**
- Modify: `factory/state_machine.py`

**Step 1: Read the current state machine**

Read `factory/state_machine.py` to understand the current WAITING references (JobStatus enum, TRANSITIONS dict, any methods referencing waiting).

**Step 2: Rename JobStatus.WAITING to JobStatus.BLOCKED**

In the `JobStatus` enum:

```python
class JobStatus(str, Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    BLOCKED = "blocked"            # Was: WAITING (conflicted on file locks)
    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"
    QA = "qa"
    FIX_LOOP = "fix_loop"
    READY_FOR_APPROVAL = "ready_for_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEPLOYED = "deployed"
    FAILED = "failed"
```

**Step 3: Update TRANSITIONS dict**

Replace the WAITING entry with a BLOCKED entry that allows three resolution paths plus safety fallback:

```python
TRANSITIONS: dict[JobStatus, list[JobStatus]] = {
    JobStatus.QUEUED: [JobStatus.PLANNING],
    JobStatus.PLANNING: [JobStatus.IMPLEMENTING, JobStatus.BLOCKED, JobStatus.FAILED],
    JobStatus.BLOCKED: [
        JobStatus.IMPLEMENTING,  # proceed resolution
        JobStatus.PLANNING,      # replan resolution
        JobStatus.REJECTED,      # cancel resolution
        JobStatus.FAILED,        # safety net (e.g., lock still held after proceed)
    ],
    JobStatus.IMPLEMENTING: [JobStatus.REVIEWING, JobStatus.FAILED],
    JobStatus.REVIEWING: [JobStatus.QA, JobStatus.FIX_LOOP, JobStatus.FAILED],
    JobStatus.QA: [JobStatus.READY_FOR_APPROVAL, JobStatus.FIX_LOOP, JobStatus.FAILED],
    JobStatus.FIX_LOOP: [JobStatus.IMPLEMENTING, JobStatus.FAILED],
    JobStatus.READY_FOR_APPROVAL: [JobStatus.APPROVED, JobStatus.REJECTED],
    JobStatus.APPROVED: [JobStatus.DEPLOYED],
    JobStatus.REJECTED: [],
    JobStatus.DEPLOYED: [],
    JobStatus.FAILED: [JobStatus.QUEUED],
}
```

**Step 4: Add `blocked_resolution` to FactoryJob dataclass**

Find the `FactoryJob` dataclass and add `blocked_resolution: str | None = None` field.

**Step 5: Update get_job and list_jobs SELECT statements**

Both currently select 18 columns (id, project_id, project_slug, title, description, spec, status, priority, branch_name, current_phase, error_count, max_retries, assigned_cli, metadata, created_at, updated_at, submitted_by, blocked_by_job_id). Add `j.blocked_resolution` as the 19th column. Update the FactoryJob(...) construction to include it.

**Step 6: Add `set_blocked_resolution` and `clear_blocked_resolution` methods to FactoryDB**

Add these after the existing transition method:

```python
    def set_blocked_resolution(self, job_id: str, resolution: str) -> None:
        """Set the dev's resolution for a blocked job.
        
        resolution: 'proceed' | 'replan' | 'cancel'
        """
        if resolution not in ("proceed", "replan", "cancel"):
            raise ValueError(f"Invalid resolution: {resolution}")
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE devbrain.factory_jobs SET blocked_resolution = %s, updated_at = now() WHERE id = %s",
                (resolution, job_id),
            )
            conn.commit()

    def clear_blocked_resolution(self, job_id: str) -> None:
        """Clear the blocked_resolution field (called after factory consumes it)."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE devbrain.factory_jobs SET blocked_resolution = NULL, updated_at = now() WHERE id = %s",
                (job_id,),
            )
            conn.commit()
```

**Step 7: Update the test file**

Rename `factory/tests/test_state_machine_waiting.py` → `factory/tests/test_state_machine_blocked.py` and update its content:

```python
"""Tests for BLOCKED state transitions."""
import pytest
from state_machine import FactoryDB, JobStatus, TRANSITIONS

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


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
    job_id = db.create_job(project_slug="devbrain", title="Test BLOCKED", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    job = db.transition(job_id, JobStatus.BLOCKED)
    assert job.status == JobStatus.BLOCKED


def test_blocked_resolution_field(db):
    """FactoryJob has blocked_resolution field, defaults to None."""
    job_id = db.create_job(project_slug="devbrain", title="Resolution test", spec="Test")
    job = db.get_job(job_id)
    assert hasattr(job, "blocked_resolution")
    assert job.blocked_resolution is None


def test_set_and_clear_blocked_resolution(db):
    job_id = db.create_job(project_slug="devbrain", title="Set res", spec="Test")
    db.set_blocked_resolution(job_id, "proceed")
    job = db.get_job(job_id)
    assert job.blocked_resolution == "proceed"
    db.clear_blocked_resolution(job_id)
    job = db.get_job(job_id)
    assert job.blocked_resolution is None


def test_set_invalid_resolution_raises(db):
    job_id = db.create_job(project_slug="devbrain", title="Invalid res", spec="Test")
    with pytest.raises(ValueError, match="Invalid resolution"):
        db.set_blocked_resolution(job_id, "bogus")
```

**Step 8: Run tests**

```bash
cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_state_machine_blocked.py -v
```

Also run the full suite — other tests may be referencing `JobStatus.WAITING` and need updating.

**Step 9: Update any other files referencing WAITING**

Search for `WAITING` / `"waiting"` references across the codebase:

```bash
cd /Users/patrickkelly/devbrain && grep -rn "WAITING\|\"waiting\"" factory/ mcp-server/src/ --include="*.py" --include="*.ts"
```

Update each hit to use BLOCKED / "blocked". Expect hits in: orchestrator.py, cleanup_agent.py, test files, and possibly mcp-server/src/index.ts.

**Step 10: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/state_machine.py factory/tests/test_state_machine_blocked.py
git rm factory/tests/test_state_machine_waiting.py  # If the rename wasn't automatic
git commit -m "feat: rename WAITING state to BLOCKED with new resolution transitions"
```

---

## Task 3: Cleanup Agent — Add investigate_block method

**Files:**
- Modify: `factory/cleanup_agent.py`
- Test: `factory/tests/test_cleanup_agent_block.py`

**Step 1: Write the failing test**

Create `factory/tests/test_cleanup_agent_block.py`:

```python
"""Tests for cleanup agent's block investigation."""
import pytest
from unittest.mock import patch
from state_machine import FactoryDB, JobStatus
from cleanup_agent import CleanupAgent
from file_registry import FileRegistry

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture(autouse=True)
def cleanup(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM devbrain.file_locks WHERE dev_id LIKE 'test_block_%'")
        cur.execute("""
            SELECT id FROM devbrain.factory_jobs
            WHERE title LIKE '%blockinv_%'
        """)
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            cur.execute("DELETE FROM devbrain.factory_cleanup_reports WHERE job_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM devbrain.factory_artifacts WHERE job_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM devbrain.factory_jobs WHERE id = ANY(%s)", (ids,))
        conn.commit()


def test_investigate_block_returns_cleanup_report(db):
    """investigate_block returns a CleanupReport with report_type='blocked_investigation'."""
    # Create a blocking job (holds lock)
    blocker_id = db.create_job(project_slug="devbrain", title="blockinv_blocker", spec="Test")
    registry = FileRegistry(db)
    registry.acquire_locks(
        blocker_id,
        db.get_job(blocker_id).project_id,
        ["src/shared_block.py"],
        dev_id="alice",
    )

    # Create a blocked job
    blocked_id = db.create_job(project_slug="devbrain", title="blockinv_blocked", spec="Test")
    db.transition(blocked_id, JobStatus.PLANNING)
    db.store_artifact(blocked_id, "planning", "plan_doc", "Plan: modify src/shared_block.py")
    db.transition(blocked_id, JobStatus.BLOCKED)

    conflicts = [{"file_path": "src/shared_block.py", "blocking_job_id": blocker_id}]

    agent = CleanupAgent(db)
    job = db.get_job(blocked_id)
    report = agent.investigate_block(job, conflicts)

    assert report.report_type == "blocked_investigation"
    assert report.outcome == "awaiting_resolution"
    assert "src/shared_block.py" in report.summary
    assert "blockinv_blocker" in report.summary
    assert report.metadata.get("blocking_job_id") == blocker_id
    assert report.metadata.get("recommendation") in ("proceed", "replan", "cancel")


def test_investigate_block_persists_report(db):
    """The investigation report is saved to factory_cleanup_reports."""
    blocker_id = db.create_job(project_slug="devbrain", title="blockinv_blocker2", spec="Test")
    registry = FileRegistry(db)
    registry.acquire_locks(
        blocker_id, db.get_job(blocker_id).project_id,
        ["src/shared2.py"], dev_id="alice",
    )

    blocked_id = db.create_job(project_slug="devbrain", title="blockinv_blocked2", spec="Test")
    db.transition(blocked_id, JobStatus.PLANNING)
    db.store_artifact(blocked_id, "planning", "plan_doc", "Plan: edit src/shared2.py")
    db.transition(blocked_id, JobStatus.BLOCKED)

    conflicts = [{"file_path": "src/shared2.py", "blocking_job_id": blocker_id}]
    agent = CleanupAgent(db)
    agent.investigate_block(db.get_job(blocked_id), conflicts)

    reports = db.get_cleanup_reports(blocked_id)
    assert any(r["report_type"] == "blocked_investigation" for r in reports)


def test_investigate_block_includes_blocking_job_details(db):
    """The summary includes info about the blocking job and dev."""
    blocker_id = db.create_job(
        project_slug="devbrain", title="blockinv_blocker3", spec="Test",
    )
    # Set submitted_by on the blocker
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.factory_jobs SET submitted_by = %s WHERE id = %s",
            ("alice", blocker_id),
        )
        conn.commit()

    blocked_id = db.create_job(project_slug="devbrain", title="blockinv_blocked3", spec="Test")
    db.transition(blocked_id, JobStatus.PLANNING)
    db.transition(blocked_id, JobStatus.BLOCKED)

    conflicts = [{"file_path": "src/x.py", "blocking_job_id": blocker_id}]
    agent = CleanupAgent(db)
    report = agent.investigate_block(db.get_job(blocked_id), conflicts)

    # Summary should mention the blocking dev and job
    assert "alice" in report.summary or "blockinv_blocker3" in report.summary
    assert report.metadata.get("blocking_dev_id") == "alice"
```

**Step 2: Run the test to verify failure**

```bash
cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_cleanup_agent_block.py -v
```

Expected: FAIL — `investigate_block` method doesn't exist

**Step 3: Implement `investigate_block` in cleanup_agent.py**

Add this method to the `CleanupAgent` class (after `attempt_recovery`):

```python
    def investigate_block(self, job: FactoryJob, conflicts: list[dict]) -> CleanupReport:
        """Mode 3 — called when a job transitions to BLOCKED.

        Analyzes why the job is blocked, examines the blocking job's state,
        evaluates whether the blocked job's plan is still viable, and recommends
        a resolution action (proceed / replan / cancel).

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

        # Analyze whether the blocked job's plan is likely still valid
        # Heuristic: if any blocking job is still active (not terminal),
        # we don't know yet what they'll change. Recommend coordination first.
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
            recommendation = "wait"  # Coordinate with the other dev first
            rationale = (
                f"Blocking job is still active. Coordinate with the other dev "
                f"to determine the right resolution."
            )
        elif completed_blockers:
            # Blocking job already finished — check if it modified the same files
            recommendation = "replan"
            rationale = (
                f"Blocking job has already completed. Since the codebase has "
                f"changed, replanning is strongly recommended to ensure your "
                f"plan still matches the current code."
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

        # Persist the investigation report
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
            phases_traversed=[],  # Not relevant for block investigation
            artifacts_summary={},
            recovery_diagnosis=rationale,
            recovery_action_taken=f"recommendation: {recommendation}",
            time_elapsed_seconds=elapsed,
            metadata=metadata,
        )
```

**Step 4: Update `_notification_title` to include `blocked` event type**

Add `"blocked"` to the title mapping:

```python
            "blocked": f"🔒 Job blocked: {job.title}",
```

**Step 5: Run tests**

```bash
cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_cleanup_agent_block.py -v
```

Expected: All tests PASS.

**Step 6: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/cleanup_agent.py factory/tests/test_cleanup_agent_block.py
git commit -m "feat: add cleanup agent investigate_block method with recommendation"
```

---

## Task 4: Orchestrator — Rip out polling, add BLOCKED handler and investigation

**Files:**
- Modify: `factory/orchestrator.py`

**Step 1: Remove `_run_waiting` method**

Delete the entire `_run_waiting` method from orchestrator.py. It's being replaced by `_run_blocked`.

**Step 2: Update `run_job` loop**

Replace the `elif job.status == JobStatus.WAITING:` branch with `elif job.status == JobStatus.BLOCKED:` and add an exit condition for BLOCKED without resolution:

```python
            if job.status == JobStatus.QUEUED:
                job = self._run_planning(job)
            elif job.status == JobStatus.BLOCKED:
                job = self._run_blocked(job)
                if job.status == JobStatus.BLOCKED:
                    # Still blocked after handler (no resolution set) — exit cleanly
                    logger.info("Job %s still blocked, exiting factory process", job.id[:8])
                    return job
            elif job.status == JobStatus.IMPLEMENTING:
                # ... rest unchanged
```

The key insight: if a job enters BLOCKED with no resolution set, `_run_blocked` returns the job unchanged. We then break out of the while loop and let the factory process exit. A new factory process will be spawned later when the dev sets a resolution.

**Step 3: Add `_run_blocked` method**

Place this method where `_run_waiting` used to be:

```python
    # ─── Blocked Phase ─────────────────────────────────────────────────────

    def _run_blocked(self, job: FactoryJob) -> FactoryJob:
        """Blocked phase: job is waiting for dev resolution.

        Checks for a resolution set by the dev (via MCP tool or CLI). If set,
        executes the resolution (cancel / proceed / replan). If not set,
        returns the job unchanged — the factory process will exit and a new
        one will be spawned when a resolution arrives.
        """
        resolution = job.blocked_resolution
        if not resolution:
            logger.info(
                "Job %s is BLOCKED with no resolution — factory will exit",
                job.id[:8],
            )
            return job  # No change — caller will break out of loop

        logger.info("Job %s has resolution '%s', executing...", job.id[:8], resolution)

        # Clear the resolution field so it's consumed exactly once
        self.db.clear_blocked_resolution(job.id)

        if resolution == "cancel":
            return self._resolve_cancel(job)
        elif resolution == "proceed":
            return self._resolve_proceed(job)
        elif resolution == "replan":
            return self._resolve_replan(job)
        else:
            logger.warning("Unknown resolution '%s' for job %s, ignoring", resolution, job.id[:8])
            return job

    def _resolve_cancel(self, job: FactoryJob) -> FactoryJob:
        """Cancel a blocked job: release locks, transition to REJECTED."""
        from file_registry import FileRegistry

        registry = FileRegistry(self.db)
        registry.release_locks(job.id)

        # Clear blocked_by_job_id
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE devbrain.factory_jobs SET blocked_by_job_id = NULL WHERE id = %s",
                (job.id,),
            )
            conn.commit()

        return self.db.transition(
            job.id, JobStatus.REJECTED,
            metadata={"rejected_reason": "dev resolution: cancel (from BLOCKED)"},
        )

    def _resolve_proceed(self, job: FactoryJob) -> FactoryJob:
        """Proceed with original plan: acquire locks (if free), transition to IMPLEMENTING."""
        from file_registry import FileRegistry
        from plan_parser import extract_files_from_plan

        # Re-acquire locks (they should be free if the dev resolved correctly)
        artifacts = self.db.get_artifacts(job.id, phase="planning")
        plan_artifact = next(
            (a for a in artifacts if a["artifact_type"] == "plan_doc"),
            None,
        )
        plan_files = extract_files_from_plan(plan_artifact["content"]) if plan_artifact else []

        registry = FileRegistry(self.db)
        lock_result = registry.acquire_locks(
            job_id=job.id,
            project_id=job.project_id,
            file_paths=plan_files,
            dev_id=job.submitted_by,
        )

        if not lock_result.success:
            logger.warning(
                "Job %s proceed resolution failed — locks still held by %s",
                job.id[:8],
                lock_result.conflicts,
            )
            # Stay blocked; dev can retry later
            self.db.store_artifact(
                job_id=job.id,
                phase="blocked",
                artifact_type="proceed_failed",
                content=json.dumps({
                    "reason": "locks still held",
                    "conflicts": lock_result.conflicts,
                }),
            )
            return job  # Still BLOCKED

        # Clear blocked_by_job_id
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE devbrain.factory_jobs SET blocked_by_job_id = NULL WHERE id = %s",
                (job.id,),
            )
            conn.commit()

        # Create branch (same logic as _run_planning success path)
        project_root = self._get_project_root(job)
        slug = re.sub(r'[^a-z0-9-]', '-', job.title.lower())[:40].strip('-')
        branch = f"factory/{job.id[:8]}/{slug}"
        try:
            subprocess.run(
                ["git", "checkout", "-b", branch],
                cwd=project_root, capture_output=True, timeout=10,
            )
        except Exception as e:
            logger.warning("Branch creation failed: %s", e)
            branch = None

        # Fire unblocked notification
        self._fire_unblocked_notification(job)

        return self.db.transition(
            job.id, JobStatus.IMPLEMENTING, branch_name=branch,
        )

    def _resolve_replan(self, job: FactoryJob) -> FactoryJob:
        """Replan resolution: transition back to PLANNING so the plan phase re-runs
        with the updated codebase.
        """
        # Release any stale file locks from the original plan — they'll be
        # re-acquired when the new plan is generated.
        from file_registry import FileRegistry
        registry = FileRegistry(self.db)
        registry.release_locks(job.id)

        # Clear blocked_by_job_id
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE devbrain.factory_jobs SET blocked_by_job_id = NULL WHERE id = %s",
                (job.id,),
            )
            conn.commit()

        logger.info("Job %s replan resolution — returning to PLANNING", job.id[:8])
        return self.db.transition(
            job.id, JobStatus.PLANNING,
            metadata={"replan_reason": "dev resolution from BLOCKED state"},
        )

    def _fire_unblocked_notification(self, job: FactoryJob) -> None:
        """Helper: fire unblocked notification. Used by _resolve_proceed."""
        try:
            from notifications.router import NotificationRouter, NotificationEvent
            if job.submitted_by:
                router = NotificationRouter(self.db)
                router.send(NotificationEvent(
                    event_type="unblocked",
                    recipient_dev_id=job.submitted_by,
                    title=f"🔓 Job unblocked: {job.title}",
                    body="Your job is no longer blocked on file locks and is now implementing.",
                    job_id=job.id,
                ))
        except Exception as e:
            logger.warning("Unblock notification failed: %s", e)
```

**Step 4: Update `_run_planning` — call `investigate_block` + fire notification with report**

Find the section of `_run_planning` that handles lock conflicts (where it currently transitions to WAITING / now BLOCKED). Replace with:

```python
            if not lock_result.success:
                # File conflicts — investigate and transition to BLOCKED
                blocking_job_id = (
                    lock_result.conflicts[0]["blocking_job_id"]
                    if lock_result.conflicts else None
                )

                logger.info(
                    "Job %s has %d file conflicts — investigating",
                    job.id[:8], len(lock_result.conflicts),
                )

                # Set blocked_by_job_id via direct SQL (transition doesn't support it)
                with self.db._conn() as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE devbrain.factory_jobs SET blocked_by_job_id = %s WHERE id = %s",
                        (blocking_job_id, job.id),
                    )
                    conn.commit()

                # Transition first so investigate_block sees the correct status
                self.db.store_artifact(
                    job_id=job.id,
                    phase="planning",
                    artifact_type="lock_conflicts",
                    content=json.dumps(lock_result.conflicts, indent=2),
                )
                blocked_job = self.db.transition(
                    job.id, JobStatus.BLOCKED,
                    metadata={"lock_conflicts": lock_result.conflicts},
                )

                # Run cleanup agent investigation — builds the findings report
                from cleanup_agent import CleanupAgent
                agent = CleanupAgent(self.db)
                try:
                    report = agent.investigate_block(blocked_job, lock_result.conflicts)
                except Exception as e:
                    logger.warning("Block investigation failed (non-blocking): %s", e)
                    report = None

                # Fire notification with the investigation report as the body
                try:
                    from notifications.router import NotificationRouter, NotificationEvent
                    router = NotificationRouter(self.db)
                    if blocked_job.submitted_by:
                        notification_body = (
                            report.summary if report
                            else (
                                "Your job is blocked on file lock conflicts.\n\n"
                                f"Conflicting files: {', '.join(c['file_path'] for c in lock_result.conflicts)}"
                            )
                        )
                        metadata = {
                            "blocking_job_id": blocking_job_id,
                            "conflicts": lock_result.conflicts,
                        }
                        if report:
                            metadata["blocking_dev_id"] = report.metadata.get("blocking_dev_id")
                            metadata["recommendation"] = report.metadata.get("recommendation")
                        router.send_multi(NotificationEvent(
                            event_type="blocked",
                            recipient_dev_id=blocked_job.submitted_by,
                            title=f"🔒 Job blocked: {job.title}",
                            body=notification_body,
                            job_id=job.id,
                            metadata=metadata,
                        ))
                except Exception as e:
                    logger.warning("Block notification failed: %s", e)

                return blocked_job
```

**Step 5: Update router's `send_multi` to handle `blocked` event type**

The router currently handles `lock_conflict` multi-dev notification. Add similar handling for `blocked`:

In `factory/notifications/router.py`, update `send_multi`:

```python
    def send_multi(self, event: NotificationEvent) -> list[RouterResult]:
        """For events that notify multiple devs (lock conflicts, blocks)."""
        results = [self.send(event)]

        # Both lock_conflict and blocked events notify the blocking dev as well
        if event.event_type in ("lock_conflict", "blocked"):
            blocking_dev_id = event.metadata.get("blocking_dev_id")
            if blocking_dev_id and blocking_dev_id != event.recipient_dev_id:
                title_prefix = (
                    "Your job is blocking another dev's job"
                    if event.event_type == "blocked"
                    else f"Your job is blocking {event.recipient_dev_id}"
                )
                blocker_event = NotificationEvent(
                    event_type=event.event_type,
                    recipient_dev_id=blocking_dev_id,
                    title=title_prefix,
                    body=(
                        f"Another dev's factory job is waiting for you to finish "
                        f"because you're holding file locks they need.\n\n"
                        f"{event.body}"
                    ),
                    job_id=event.job_id,
                    metadata=event.metadata,
                )
                results.append(self.send(blocker_event))

        return results
```

**Step 6: Add `"blocked"` event type to defaults**

In `factory/state_machine.py`, update `DEFAULT_EVENT_SUBSCRIPTIONS` to include `"blocked"`:

```python
    DEFAULT_EVENT_SUBSCRIPTIONS = [
        "job_started",
        "job_ready",
        "job_failed",
        "blocked",                # NEW
        "lock_conflict",
        "unblocked",
        "needs_human",
        "recovery_started",
        "recovery_succeeded",
    ]
```

Also add `"blocked"` to `notify_events` in `config/devbrain.yaml.example` and `config/devbrain.yaml`.

**Step 7: Run all tests**

```bash
cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/ -v
```

Expected: All tests pass. The orchestrator no longer has `_run_waiting`, so any test referencing it needs updating.

**Step 8: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/orchestrator.py factory/notifications/router.py factory/state_machine.py config/devbrain.yaml.example config/devbrain.yaml
git commit -m "feat: replace WAITING polling with BLOCKED + cleanup agent investigation"
```

---

## Task 5: MCP Tool — devbrain_resolve_blocked

**Files:**
- Modify: `mcp-server/src/index.ts`

**Step 1: Add the tool after `factory_cleanup`**

```typescript
// ─── Tool: devbrain_resolve_blocked ──────────────────────────────────────

server.tool(
  'devbrain_resolve_blocked',
  'Resolve a blocked factory job. Call after investigating via factory_status/factory_file_locks and discussing with the dev. Sets the resolution and spawns a factory process to execute it.',
  {
    job_id: z.string().describe('Job ID (full or first 8 chars)'),
    action: z.enum(['proceed', 'replan', 'cancel']).describe(
      'proceed: use original plan once locks free. replan: re-run planning with updated code. cancel: kill the job.'
    ),
    notes: z.string().optional().describe('Optional notes about why this decision was made'),
  },
  async ({ job_id, action, notes }) => {
    // Resolve short job_id to full UUID if needed
    let fullJobId = job_id
    if (job_id.length < 32) {
      const result = await query<{ id: string }>(
        "SELECT id FROM devbrain.factory_jobs WHERE id::text LIKE $1 AND status = 'blocked' LIMIT 1",
        [`${job_id}%`],
      )
      if (result.rows.length === 0) {
        return {
          content: [{
            type: 'text',
            text: `No blocked job found matching "${job_id}".`,
          }],
        }
      }
      fullJobId = result.rows[0].id
    }

    // Verify the job exists and is blocked
    const job = await query(
      "SELECT id, title, status, submitted_by FROM devbrain.factory_jobs WHERE id = $1",
      [fullJobId],
    )

    if (job.rows.length === 0) {
      return {
        content: [{
          type: 'text',
          text: `Job ${fullJobId} not found.`,
        }],
      }
    }

    const status = job.rows[0].status as string
    const title = job.rows[0].title as string

    if (status !== 'blocked') {
      return {
        content: [{
          type: 'text',
          text: `Job "${title}" is not blocked (status: ${status}). Cannot apply resolution.`,
        }],
      }
    }

    // Write resolution + notes to the DB
    await query(
      `UPDATE devbrain.factory_jobs
       SET blocked_resolution = $1,
           metadata = metadata || jsonb_build_object('resolution_notes', $2::text),
           updated_at = now()
       WHERE id = $3`,
      [action, notes ?? '', fullJobId],
    )

    // Spawn a detached factory process to execute the resolution
    try {
      const factoryPython = resolve(import.meta.dirname, '../../.venv/bin/python')
      const child = spawn(factoryPython, [FACTORY_RUNNER, fullJobId], {
        detached: true,
        stdio: ['ignore', 'pipe', 'pipe'],
        cwd: resolve(import.meta.dirname, '../..'),
      })
      child.unref()
      console.error(`[factory] Spawned resolver for blocked job ${fullJobId.slice(0, 8)} (pid ${child.pid})`)
    } catch (err) {
      return {
        content: [{
          type: 'text',
          text: `Resolution "${action}" saved but factory spawn failed: ${err}. Run manually: python factory/run.py ${fullJobId}`,
        }],
      }
    }

    return {
      content: [{
        type: 'text',
        text: `✅ Resolution "${action}" applied to job "${title}" (${fullJobId.slice(0, 8)}). Factory process spawned to execute.`,
      }],
    }
  },
)
```

**Step 2: Build the MCP server**

```bash
cd /Users/patrickkelly/devbrain/mcp-server && npm run build
```

Expected: Clean compile.

**Step 3: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add mcp-server/src/index.ts
git commit -m "feat: add devbrain_resolve_blocked MCP tool"
```

---

## Task 6: CLI — blocked list and resolve commands

**Files:**
- Modify: `factory/cli.py`
- Test: `factory/tests/test_cli_blocked.py`

**Step 1: Add the commands**

In `factory/cli.py`, add these commands after the existing `watch` command:

```python
@cli.command(name="blocked")
@click.option("--project", default=None, help="Filter by project slug")
def blocked(project):
    """List all currently blocked factory jobs."""
    db = get_db()

    with db._conn() as conn, conn.cursor() as cur:
        sql = """
            SELECT j.id, j.title, j.submitted_by, j.blocked_by_job_id,
                   j.updated_at, p.slug
            FROM devbrain.factory_jobs j
            JOIN devbrain.projects p ON j.project_id = p.id
            WHERE j.status = 'blocked'
        """
        params = []
        if project:
            sql += " AND p.slug = %s"
            params.append(project)
        sql += " ORDER BY j.updated_at DESC"
        cur.execute(sql, params)
        rows = cur.fetchall()

    if not rows:
        click.echo("No blocked jobs.")
        return

    for r in rows:
        job_id, title, submitted_by, blocked_by, updated_at, slug = r
        click.echo(f"\n🔒 {title} [{slug}]")
        click.echo(f"   ID: {str(job_id)[:8]}")
        click.echo(f"   Submitted by: {submitted_by or '(unknown)'}")
        click.echo(f"   Blocked by job: {str(blocked_by)[:8] if blocked_by else '(unknown)'}")
        click.echo(f"   Blocked at: {updated_at}")


@cli.command(name="resolve")
@click.argument("job_id")
@click.option("--proceed", "action", flag_value="proceed", help="Use original plan")
@click.option("--replan", "action", flag_value="replan", help="Re-run planning with updated codebase")
@click.option("--cancel", "action", flag_value="cancel", help="Cancel the job")
@click.option("--notes", default=None, help="Optional notes about why")
def resolve(job_id, action, notes):
    """Resolve a blocked job."""
    if not action:
        click.echo("Error: must specify --proceed, --replan, or --cancel", err=True)
        sys.exit(1)

    db = get_db()

    # Resolve short job_id to full UUID
    with db._conn() as conn, conn.cursor() as cur:
        if len(job_id) < 32:
            cur.execute(
                "SELECT id, title FROM devbrain.factory_jobs WHERE id::text LIKE %s AND status = 'blocked' LIMIT 1",
                (f"{job_id}%",),
            )
        else:
            cur.execute(
                "SELECT id, title FROM devbrain.factory_jobs WHERE id = %s",
                (job_id,),
            )
        row = cur.fetchone()

    if not row:
        click.echo(f"No blocked job found matching '{job_id}'.", err=True)
        sys.exit(1)

    full_id, title = row
    full_id = str(full_id)

    # Set the resolution
    db.set_blocked_resolution(full_id, action)

    # Add notes if provided
    if notes:
        import json as _json
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE devbrain.factory_jobs
                   SET metadata = metadata || %s::jsonb
                   WHERE id = %s""",
                (_json.dumps({"resolution_notes": notes}), full_id),
            )
            conn.commit()

    click.echo(f"✅ Resolution '{action}' set for job '{title}' ({full_id[:8]})")

    # Spawn factory process to execute
    import subprocess
    factory_runner = str(Path(__file__).parent / "run.py")
    python_bin = str(Path(__file__).parent.parent / ".venv" / "bin" / "python")
    try:
        subprocess.Popen(
            [python_bin, factory_runner, full_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        click.echo(f"   Factory process spawned to execute resolution.")
    except Exception as e:
        click.echo(f"   ⚠️  Failed to spawn factory: {e}", err=True)
        click.echo(f"   Run manually: {python_bin} {factory_runner} {full_id}")
```

**Step 2: Write the test**

Create `factory/tests/test_cli_blocked.py`:

```python
"""Tests for the blocked and resolve CLI commands."""
import pytest
from click.testing import CliRunner
from cli import cli
from state_machine import FactoryDB, JobStatus

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture(autouse=True)
def cleanup(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM devbrain.factory_jobs WHERE title LIKE '%cli_blocked_test_%'")
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            cur.execute("DELETE FROM devbrain.factory_cleanup_reports WHERE job_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM devbrain.factory_artifacts WHERE job_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM devbrain.factory_jobs WHERE id = ANY(%s)", (ids,))
        conn.commit()


@pytest.fixture
def runner():
    return CliRunner()


def test_blocked_command_lists_blocked_jobs(runner, db):
    job_id = db.create_job(project_slug="devbrain", title="cli_blocked_test_1", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.BLOCKED)

    result = runner.invoke(cli, ["blocked"])
    assert result.exit_code == 0
    assert "cli_blocked_test_1" in result.output


def test_blocked_command_empty(runner):
    # Filter to a non-existent project to ensure empty output
    result = runner.invoke(cli, ["blocked", "--project", "nonexistent_project_xyz"])
    assert result.exit_code == 0
    assert "No blocked jobs" in result.output


def test_resolve_proceed_sets_field(runner, db, monkeypatch):
    # Mock subprocess.Popen so we don't actually spawn the factory
    import subprocess as _sub
    monkeypatch.setattr(_sub, "Popen", lambda *a, **kw: None)

    job_id = db.create_job(project_slug="devbrain", title="cli_blocked_test_2", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.BLOCKED)

    result = runner.invoke(cli, ["resolve", job_id[:8], "--proceed"])
    assert result.exit_code == 0, result.output
    assert "proceed" in result.output.lower()

    job = db.get_job(job_id)
    assert job.blocked_resolution == "proceed"


def test_resolve_replan_sets_field(runner, db, monkeypatch):
    import subprocess as _sub
    monkeypatch.setattr(_sub, "Popen", lambda *a, **kw: None)

    job_id = db.create_job(project_slug="devbrain", title="cli_blocked_test_3", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.BLOCKED)

    result = runner.invoke(cli, ["resolve", job_id[:8], "--replan"])
    assert result.exit_code == 0
    job = db.get_job(job_id)
    assert job.blocked_resolution == "replan"


def test_resolve_cancel_sets_field(runner, db, monkeypatch):
    import subprocess as _sub
    monkeypatch.setattr(_sub, "Popen", lambda *a, **kw: None)

    job_id = db.create_job(project_slug="devbrain", title="cli_blocked_test_4", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.BLOCKED)

    result = runner.invoke(cli, ["resolve", job_id[:8], "--cancel"])
    assert result.exit_code == 0
    job = db.get_job(job_id)
    assert job.blocked_resolution == "cancel"


def test_resolve_requires_action_flag(runner, db):
    job_id = db.create_job(project_slug="devbrain", title="cli_blocked_test_5", spec="Test")
    result = runner.invoke(cli, ["resolve", job_id[:8]])
    assert result.exit_code != 0


def test_resolve_nonexistent_job(runner):
    result = runner.invoke(cli, ["resolve", "nonexistent", "--proceed"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower() or "no blocked" in result.output.lower()
```

**Step 3: Run tests**

```bash
cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_cli_blocked.py -v
```

Expected: All tests PASS.

**Step 4: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/cli.py factory/tests/test_cli_blocked.py
git commit -m "feat: add devbrain blocked and resolve CLI commands"
```

---

## Task 7: Update multi-dev integration tests

**Files:**
- Modify: `factory/tests/test_multi_dev_integration.py`
- Modify: `factory/tests/test_file_registry.py` (only if it has waiting refs)
- Any other test files referencing WAITING

**Step 1: Find all references**

```bash
cd /Users/patrickkelly/devbrain && grep -rn "WAITING\|\"waiting\"" factory/tests/
```

**Step 2: Update each reference**

Change `JobStatus.WAITING` → `JobStatus.BLOCKED` and `"waiting"` → `"blocked"` in all test assertions.

**Step 3: Remove or update tests that relied on polling behavior**

The old behavior: job would auto-unblock when locks cleared. The new behavior: job stays blocked until dev resolution. Any test asserting auto-unblock needs to either:
- Set `blocked_resolution = "proceed"` before checking, or
- Assert that the job stays BLOCKED without resolution

**Step 4: Run all tests**

```bash
cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/ -v
```

Expected: All tests pass.

**Step 5: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/tests/
git commit -m "test: update multi-dev and file registry tests for BLOCKED state"
```

---

## Task 8: End-to-end integration test for blocked → resolve flow

**Files:**
- Create: `factory/tests/test_blocked_resolution_flow.py`

**Step 1: Write the test**

```python
"""End-to-end tests for blocked → investigate → resolve flow."""
import pytest
from unittest.mock import patch
from state_machine import FactoryDB, JobStatus
from cleanup_agent import CleanupAgent
from file_registry import FileRegistry
from orchestrator import FactoryOrchestrator

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture(autouse=True)
def cleanup(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM devbrain.file_locks WHERE dev_id LIKE 'test_bflow_%'")
        cur.execute("SELECT id FROM devbrain.factory_jobs WHERE title LIKE '%bflow_%'")
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            cur.execute("DELETE FROM devbrain.factory_cleanup_reports WHERE job_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM devbrain.factory_artifacts WHERE job_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM devbrain.factory_jobs WHERE id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM devbrain.devs WHERE dev_id LIKE 'test_bflow_%'")
        cur.execute("DELETE FROM devbrain.notifications WHERE recipient_dev_id LIKE 'test_bflow_%'")
        conn.commit()


def test_blocked_flow_investigation_creates_report(db):
    """When a job transitions to BLOCKED, investigation runs and report is stored."""
    # Blocker job with an active lock
    blocker_id = db.create_job(project_slug="devbrain", title="bflow_blocker", spec="Test")
    registry = FileRegistry(db)
    registry.acquire_locks(
        blocker_id, db.get_job(blocker_id).project_id,
        ["src/bflow_shared.py"], dev_id="alice",
    )

    # Blocked job
    blocked_id = db.create_job(project_slug="devbrain", title="bflow_blocked_inv", spec="Test")
    db.transition(blocked_id, JobStatus.PLANNING)
    db.store_artifact(blocked_id, "planning", "plan_doc", "Modify src/bflow_shared.py")
    db.transition(blocked_id, JobStatus.BLOCKED)

    agent = CleanupAgent(db)
    agent.investigate_block(
        db.get_job(blocked_id),
        [{"file_path": "src/bflow_shared.py", "blocking_job_id": blocker_id}],
    )

    reports = db.get_cleanup_reports(blocked_id)
    block_reports = [r for r in reports if r["report_type"] == "blocked_investigation"]
    assert len(block_reports) >= 1
    assert "bflow_blocker" in block_reports[0]["summary"]


def test_resolve_cancel_transitions_to_rejected(db):
    """Setting resolution=cancel and running factory transitions to REJECTED."""
    # Register dev
    db.register_dev(dev_id="test_bflow_carol", channels=[])

    job_id = db.create_job(project_slug="devbrain", title="bflow_cancel_test", spec="Test")
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.factory_jobs SET submitted_by = %s WHERE id = %s",
            ("test_bflow_carol", job_id),
        )
        conn.commit()
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.BLOCKED)
    db.set_blocked_resolution(job_id, "cancel")

    orch = FactoryOrchestrator(DATABASE_URL)
    job = db.get_job(job_id)
    result = orch._run_blocked(job)

    assert result.status == JobStatus.REJECTED
    # Resolution should be cleared
    reloaded = db.get_job(job_id)
    assert reloaded.blocked_resolution is None


def test_resolve_replan_transitions_to_planning(db):
    db.register_dev(dev_id="test_bflow_dave", channels=[])

    job_id = db.create_job(project_slug="devbrain", title="bflow_replan_test", spec="Test")
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.factory_jobs SET submitted_by = %s WHERE id = %s",
            ("test_bflow_dave", job_id),
        )
        conn.commit()
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.BLOCKED)
    db.set_blocked_resolution(job_id, "replan")

    orch = FactoryOrchestrator(DATABASE_URL)
    job = db.get_job(job_id)
    result = orch._run_blocked(job)

    assert result.status == JobStatus.PLANNING


def test_blocked_without_resolution_stays_blocked(db):
    job_id = db.create_job(project_slug="devbrain", title="bflow_no_res_test", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.BLOCKED)

    orch = FactoryOrchestrator(DATABASE_URL)
    job = db.get_job(job_id)
    result = orch._run_blocked(job)

    assert result.status == JobStatus.BLOCKED  # Unchanged


def test_blocked_notification_includes_investigation_summary(db):
    """Block notification body contains the investigation report summary."""
    db.register_dev(
        dev_id="test_bflow_eve",
        channels=[{"type": "tmux", "address": "test_bflow_eve"}],
    )

    blocker_id = db.create_job(project_slug="devbrain", title="bflow_notif_blocker", spec="Test")
    registry = FileRegistry(db)
    registry.acquire_locks(
        blocker_id, db.get_job(blocker_id).project_id,
        ["src/bflow_notif_shared.py"], dev_id="test_bflow_eve",
    )

    blocked_id = db.create_job(project_slug="devbrain", title="bflow_notif_blocked", spec="Test")
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.factory_jobs SET submitted_by = %s WHERE id = %s",
            ("test_bflow_eve", blocked_id),
        )
        conn.commit()
    db.transition(blocked_id, JobStatus.PLANNING)
    db.store_artifact(blocked_id, "planning", "plan_doc", "Modify src/bflow_notif_shared.py")
    db.transition(blocked_id, JobStatus.BLOCKED)

    # Run investigation
    agent = CleanupAgent(db)
    report = agent.investigate_block(
        db.get_job(blocked_id),
        [{"file_path": "src/bflow_notif_shared.py", "blocking_job_id": blocker_id}],
    )

    # Fire notification with the report summary as body
    from notifications.router import NotificationRouter, NotificationEvent
    router = NotificationRouter(db)
    with patch("notifications.channels.tmux.TmuxChannel._is_session_active", return_value=False):
        router.send(NotificationEvent(
            event_type="blocked",
            recipient_dev_id="test_bflow_eve",
            title=f"🔒 Job blocked: bflow_notif_blocked",
            body=report.summary,
            job_id=blocked_id,
        ))

    notifs = db.get_notifications(recipient_dev_id="test_bflow_eve", limit=5)
    assert any("bflow_notif_shared.py" in n["body"] for n in notifs)
    assert any("Recommendation" in n["body"] for n in notifs)
```

**Step 2: Run the test**

```bash
cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_blocked_resolution_flow.py -v
```

Expected: All tests PASS.

**Step 3: Run the full suite one more time**

```bash
cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/ -v
```

Expected: All tests pass. This is the final sanity check.

**Step 4: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/tests/test_blocked_resolution_flow.py
git commit -m "test: add end-to-end blocked → investigate → resolve flow tests"
```

---

## Summary

| Task | What | Depends On |
|------|------|-----------|
| 1 | DB migration: blocked_resolution, waiting→blocked | — |
| 2 | State machine rename + new transitions | 1 |
| 3 | Cleanup agent investigate_block method | 2 |
| 4 | Orchestrator: remove polling, add BLOCKED handler + investigation | 2, 3 |
| 5 | MCP tool: devbrain_resolve_blocked | 2 |
| 6 | CLI: blocked + resolve commands | 2 |
| 7 | Update existing tests (WAITING → BLOCKED) | 2 |
| 8 | End-to-end integration tests | 4 |

**Parallelization:**
- Task 2 must run after Task 1
- Tasks 3, 5, 6, 7 can all run in parallel after Task 2
- Task 4 depends on Tasks 2 and 3
- Task 8 depends on Task 4

**Design highlights:**

1. **No polling** — factory spawns, runs, exits. New process spawned on each resolution.
2. **Cleanup agent as investigator** — one place that knows how to analyze a block and build a findings report.
3. **Investigation report in notification** — dev sees the analysis immediately in their tmux popup, without waiting for AI to re-investigate.
4. **AI session as primary resolution interface** — dev asks their AI, AI reads the pre-built report via DevBrain MCP, discusses with dev, calls `devbrain_resolve_blocked`.
5. **CLI as fallback** — same backend, different interface. For scripts, AI-less devs, or escape-hatch debugging.
6. **Safe resolution semantics** — if a dev resolves too early (locks still held), the proceed path stays blocked with a helpful artifact. No silent corruption.
7. **Replan does real work** — goes back to PLANNING phase so the plan picks up the updated codebase.

**Event types affected:**
- `blocked` (new) — fired when a job is blocked. Recipient: blocked dev + blocking dev.
- `unblocked` (existing) — now only fires after `proceed` resolution acquires locks successfully.
- `lock_conflict` (existing) — will be removed or deprecated in favor of `blocked`. Both conveyed the same thing.

**Decision on lock_conflict vs blocked event types:** Keep `blocked` as the new canonical event type. Deprecate `lock_conflict` by removing it from the notify_events list but keep the code paths compatible so existing references don't break.
