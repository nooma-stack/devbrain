# Multi-Dev File Registry Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enable multiple developers at Lighthouse Therapy to run parallel factory jobs on a shared Mac Studio host, with file-level conflict detection preventing overlapping work.

**Architecture:** Add a `file_locks` table as a distributed lock registry. The planning phase already identifies which files a job will modify — we parse those from the plan artifact and register them in the lock table before implementation begins. Conflicting jobs enter a WAITING state and are automatically unblocked when the blocking job releases its locks (via the cleanup agent). A TTL on locks protects against crashed jobs.

**Tech Stack:** Python, psycopg2, PostgreSQL, existing factory orchestrator + cleanup agent

---

## Task 1: DB Migration — file_locks Table + WAITING Status

**Files:**
- Create: `migrations/004_file_registry.sql`

**Step 1: Write the migration**

```sql
-- Migration 004: File registry for multi-dev concurrency control
-- Adds file_locks table so parallel factory jobs can coordinate file access.
-- Jobs lock files during PLANNING, release during cleanup.

CREATE TABLE devbrain.file_locks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID REFERENCES devbrain.factory_jobs(id) ON DELETE CASCADE NOT NULL,
    project_id      UUID REFERENCES devbrain.projects(id) NOT NULL,
    file_path       TEXT NOT NULL,
    dev_id          VARCHAR(255),          -- SSH user who submitted the job
    locked_at       TIMESTAMPTZ DEFAULT now(),
    expires_at      TIMESTAMPTZ DEFAULT (now() + interval '2 hours'),
    UNIQUE(project_id, file_path)          -- One active lock per file per project
);

CREATE INDEX idx_file_locks_job ON devbrain.file_locks(job_id);
CREATE INDEX idx_file_locks_project ON devbrain.file_locks(project_id);
CREATE INDEX idx_file_locks_expires ON devbrain.file_locks(expires_at);

-- Add submitted_by column to factory_jobs for dev identity
ALTER TABLE devbrain.factory_jobs
    ADD COLUMN submitted_by VARCHAR(255);

-- Add blocked_by_job_id for WAITING state — tracks which job we're waiting on
ALTER TABLE devbrain.factory_jobs
    ADD COLUMN blocked_by_job_id UUID REFERENCES devbrain.factory_jobs(id);

CREATE INDEX idx_factory_jobs_blocked_by ON devbrain.factory_jobs(blocked_by_job_id)
    WHERE blocked_by_job_id IS NOT NULL;
```

**Step 2: Run the migration**

Run: `psql "postgresql://devbrain:devbrain-local@localhost:5433/devbrain" -f migrations/004_file_registry.sql`
Expected: CREATE TABLE, CREATE INDEX x3, ALTER TABLE x2, CREATE INDEX — no errors

**Step 3: Verify schema**

Run: `psql "postgresql://devbrain:devbrain-local@localhost:5433/devbrain" -c "\d devbrain.file_locks"`
Expected: Table with id, job_id, project_id, file_path, dev_id, locked_at, expires_at columns

**Step 4: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add migrations/004_file_registry.sql
git commit -m "chore: add file_locks table and submitted_by for multi-dev concurrency"
```

---

## Task 2: Add WAITING Status to State Machine

**Files:**
- Modify: `factory/state_machine.py`

**Step 1: Write the failing test**

Create: `factory/tests/test_state_machine_waiting.py`

```python
"""Tests for WAITING state transitions."""
import pytest
from state_machine import FactoryDB, JobStatus, TRANSITIONS

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


def test_waiting_status_exists():
    """JobStatus.WAITING enum value exists."""
    assert JobStatus.WAITING == "waiting"


def test_planning_can_transition_to_waiting():
    """PLANNING → WAITING is a valid transition (when file conflicts detected)."""
    assert JobStatus.WAITING in TRANSITIONS[JobStatus.PLANNING]


def test_waiting_can_transition_to_implementing():
    """WAITING → IMPLEMENTING is valid (when blocking jobs finish)."""
    assert JobStatus.IMPLEMENTING in TRANSITIONS[JobStatus.WAITING]


def test_waiting_can_transition_to_failed():
    """WAITING → FAILED is valid (timeout or explicit failure)."""
    assert JobStatus.FAILED in TRANSITIONS[JobStatus.WAITING]


def test_transition_planning_to_waiting(db):
    """Can actually transition a job from PLANNING to WAITING."""
    job_id = db.create_job(project_slug="devbrain", title="Test WAITING", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    job = db.transition(job_id, JobStatus.WAITING)
    assert job.status == JobStatus.WAITING
```

**Step 2: Run test to verify failures**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_state_machine_waiting.py -v`
Expected: FAIL — `JobStatus.WAITING` doesn't exist

**Step 3: Modify state_machine.py**

In `factory/state_machine.py`, add `WAITING` to the `JobStatus` enum (after line 33, after `FIX_LOOP`):

```python
class JobStatus(str, Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    WAITING = "waiting"            # Blocked by file lock conflicts
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

Update `TRANSITIONS` dict — add WAITING entries and update PLANNING:

```python
TRANSITIONS: dict[JobStatus, list[JobStatus]] = {
    JobStatus.QUEUED: [JobStatus.PLANNING],
    JobStatus.PLANNING: [JobStatus.IMPLEMENTING, JobStatus.WAITING, JobStatus.FAILED],
    JobStatus.WAITING: [JobStatus.IMPLEMENTING, JobStatus.FAILED],
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

Also update the `FactoryJob` dataclass (around line 57-74) to include `submitted_by` and `blocked_by_job_id`:

```python
@dataclass
class FactoryJob:
    id: str
    project_id: str
    project_slug: str
    title: str
    description: str | None
    spec: str | None
    status: JobStatus
    priority: int
    branch_name: str | None
    current_phase: str | None
    error_count: int
    max_retries: int
    assigned_cli: str | None
    metadata: dict
    created_at: datetime
    updated_at: datetime
    submitted_by: str | None = None
    blocked_by_job_id: str | None = None
```

Update the `get_job` SELECT to include the new columns (around lines 131-152):

```python
    def get_job(self, job_id: str) -> FactoryJob | None:
        """Get a job by ID."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT j.id, j.project_id, p.slug, j.title, j.description, j.spec,
                       j.status, j.priority, j.branch_name, j.current_phase,
                       j.error_count, j.max_retries, j.assigned_cli, j.metadata,
                       j.created_at, j.updated_at, j.submitted_by, j.blocked_by_job_id
                FROM devbrain.factory_jobs j
                JOIN devbrain.projects p ON j.project_id = p.id
                WHERE j.id = %s
                """,
                (job_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return FactoryJob(
                id=str(row[0]), project_id=str(row[1]), project_slug=row[2],
                title=row[3], description=row[4], spec=row[5],
                status=JobStatus(row[6]), priority=row[7],
                branch_name=row[8], current_phase=row[9],
                error_count=row[10], max_retries=row[11],
                assigned_cli=row[12], metadata=row[13] or {},
                created_at=row[14], updated_at=row[15],
                submitted_by=row[16],
                blocked_by_job_id=str(row[17]) if row[17] else None,
            )
```

Do the same update to `list_jobs` SELECT.

**Step 4: Run tests to verify they pass**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_state_machine_waiting.py -v`
Expected: 5 PASS

**Step 5: Run ALL factory tests to check for regressions**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/ -v`
Expected: All tests pass (24 previous + 5 new = 29)

**Step 6: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/state_machine.py factory/tests/test_state_machine_waiting.py
git commit -m "feat: add WAITING state and submitted_by/blocked_by_job_id to FactoryJob"
```

---

## Task 3: File Registry Module (Lock CRUD)

**Files:**
- Create: `factory/file_registry.py`
- Test: `factory/tests/test_file_registry.py`

**Step 1: Write the failing test**

Create: `factory/tests/test_file_registry.py`

```python
"""Tests for the file lock registry."""
import pytest
from state_machine import FactoryDB, JobStatus
from file_registry import FileRegistry, LockConflict

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"


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
    """Acquiring locks on files nobody has returns success."""
    files = ["src/auth.py", "tests/test_auth.py"]
    result = registry.acquire_locks(job1.id, job1.project_id, files, dev_id="alice")
    assert result.success is True
    assert result.conflicts == []


def test_acquire_locks_detects_conflicts(registry, job1, job2):
    """Acquiring locks on a file another job owns returns conflict info."""
    files = ["src/shared.py", "src/only_job1.py"]
    registry.acquire_locks(job1.id, job1.project_id, files, dev_id="alice")

    # Job2 tries to lock one shared file
    result = registry.acquire_locks(
        job2.id, job2.project_id,
        ["src/shared.py", "src/only_job2.py"],
        dev_id="bob",
    )
    assert result.success is False
    assert len(result.conflicts) == 1
    assert result.conflicts[0]["file_path"] == "src/shared.py"
    assert result.conflicts[0]["blocking_job_id"] == job1.id


def test_release_locks(registry, job1):
    """Releasing locks removes them from the registry."""
    files = ["src/foo.py", "src/bar.py"]
    registry.acquire_locks(job1.id, job1.project_id, files, dev_id="alice")
    released_count = registry.release_locks(job1.id)
    assert released_count == 2

    # Verify they're gone
    locked = registry.list_locked_files(job1.project_id)
    assert all(f["job_id"] != job1.id for f in locked)


def test_release_locks_unblocks_waiting_jobs(registry, db, job1, job2):
    """When locks are released, any files no longer held become available."""
    files = ["src/shared.py"]
    registry.acquire_locks(job1.id, job1.project_id, files, dev_id="alice")

    # Job2 attempts same file — conflict
    result = registry.acquire_locks(job2.id, job2.project_id, files, dev_id="bob")
    assert result.success is False

    # Job1 releases
    registry.release_locks(job1.id)

    # Job2 can now acquire
    result2 = registry.acquire_locks(job2.id, job2.project_id, files, dev_id="bob")
    assert result2.success is True


def test_expired_locks_cleanup(registry, db, job1):
    """Expired locks can be cleaned up."""
    files = ["src/old.py"]
    registry.acquire_locks(job1.id, job1.project_id, files, dev_id="alice")

    # Manually expire the lock
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.file_locks SET expires_at = now() - interval '1 hour' WHERE job_id = %s",
            (job1.id,),
        )
        conn.commit()

    cleaned = registry.cleanup_expired_locks()
    assert cleaned >= 1


def test_list_locked_files_for_project(registry, job1):
    """Can list all locked files for a project."""
    files = ["src/a.py", "src/b.py", "src/c.py"]
    registry.acquire_locks(job1.id, job1.project_id, files, dev_id="alice")
    locked = registry.list_locked_files(job1.project_id)
    assert len(locked) >= 3
    locked_paths = [f["file_path"] for f in locked]
    assert "src/a.py" in locked_paths
```

**Step 2: Run tests to verify they fail**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_file_registry.py -v`
Expected: FAIL — `file_registry` module doesn't exist

**Step 3: Implement file_registry.py**

Create: `factory/file_registry.py`

```python
"""File lock registry for multi-dev factory concurrency control.

Jobs acquire file locks during PLANNING (after the plan identifies files to modify).
If another active job has any of those files locked, the new job enters WAITING state.
When a job reaches a terminal state, the cleanup agent releases its locks.

Locks have a 2-hour TTL as a safety net for crashed jobs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from state_machine import FactoryDB

logger = logging.getLogger(__name__)


@dataclass
class LockConflict:
    """A conflict when trying to acquire a file lock."""
    file_path: str
    blocking_job_id: str
    blocking_dev_id: str | None
    locked_at: str


@dataclass
class LockResult:
    """Result of a lock acquisition attempt."""
    success: bool
    conflicts: list[dict] = field(default_factory=list)
    acquired_count: int = 0


class FileRegistry:
    """Manages file locks for multi-dev factory concurrency."""

    def __init__(self, db: FactoryDB):
        self.db = db

    def acquire_locks(
        self,
        job_id: str,
        project_id: str,
        file_paths: list[str],
        dev_id: str | None = None,
    ) -> LockResult:
        """Attempt to acquire locks on a set of files for a job.

        If any files are already locked by other jobs, returns failure with
        conflict details. The caller should transition the job to WAITING
        and record the blocking job.

        All-or-nothing: if any file conflicts, no locks are acquired.
        """
        if not file_paths:
            return LockResult(success=True, acquired_count=0)

        with self.db._conn() as conn, conn.cursor() as cur:
            # Clean up expired locks first
            cur.execute(
                "DELETE FROM devbrain.file_locks WHERE expires_at < now()"
            )

            # Check for conflicts
            cur.execute(
                """
                SELECT file_path, job_id, dev_id, locked_at
                FROM devbrain.file_locks
                WHERE project_id = %s
                  AND file_path = ANY(%s)
                  AND job_id != %s
                """,
                (project_id, file_paths, job_id),
            )
            conflicts = [
                {
                    "file_path": row[0],
                    "blocking_job_id": str(row[1]),
                    "blocking_dev_id": row[2],
                    "locked_at": str(row[3]),
                }
                for row in cur.fetchall()
            ]

            if conflicts:
                conn.rollback()
                logger.info(
                    "Lock acquisition failed for job %s: %d conflicts",
                    job_id[:8], len(conflicts),
                )
                return LockResult(success=False, conflicts=conflicts)

            # Insert all locks
            for file_path in file_paths:
                cur.execute(
                    """
                    INSERT INTO devbrain.file_locks
                        (job_id, project_id, file_path, dev_id)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (project_id, file_path) DO NOTHING
                    """,
                    (job_id, project_id, file_path, dev_id),
                )
            conn.commit()

            logger.info(
                "Acquired %d file locks for job %s",
                len(file_paths), job_id[:8],
            )
            return LockResult(success=True, acquired_count=len(file_paths))

    def release_locks(self, job_id: str) -> int:
        """Release all file locks held by a job. Returns number released."""
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM devbrain.file_locks WHERE job_id = %s",
                (job_id,),
            )
            count = cur.rowcount
            conn.commit()

        if count > 0:
            logger.info("Released %d file locks for job %s", count, job_id[:8])
        return count

    def list_locked_files(self, project_id: str) -> list[dict]:
        """List all currently locked files for a project."""
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT file_path, job_id, dev_id, locked_at, expires_at
                FROM devbrain.file_locks
                WHERE project_id = %s
                  AND expires_at > now()
                ORDER BY locked_at ASC
                """,
                (project_id,),
            )
            return [
                {
                    "file_path": row[0],
                    "job_id": str(row[1]),
                    "dev_id": row[2],
                    "locked_at": str(row[3]),
                    "expires_at": str(row[4]),
                }
                for row in cur.fetchall()
            ]

    def cleanup_expired_locks(self) -> int:
        """Delete locks past their TTL. Returns number cleaned up."""
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM devbrain.file_locks WHERE expires_at < now()"
            )
            count = cur.rowcount
            conn.commit()

        if count > 0:
            logger.info("Cleaned up %d expired file locks", count)
        return count

    def get_job_locks(self, job_id: str) -> list[str]:
        """Get list of file paths locked by a specific job."""
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT file_path FROM devbrain.file_locks WHERE job_id = %s",
                (job_id,),
            )
            return [row[0] for row in cur.fetchall()]


class LockConflictError(Exception):
    """Raised when a job cannot acquire required file locks."""
    def __init__(self, conflicts: list[dict]):
        self.conflicts = conflicts
        super().__init__(f"Cannot acquire locks: {len(conflicts)} conflicts")
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_file_registry.py -v`
Expected: All 6 tests PASS

**Step 5: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/file_registry.py factory/tests/test_file_registry.py
git commit -m "feat: add file registry for multi-dev concurrency control"
```

---

## Task 4: Plan Artifact File Extraction

**Files:**
- Create: `factory/plan_parser.py`
- Test: `factory/tests/test_plan_parser.py`

**Step 1: Write the failing test**

Create: `factory/tests/test_plan_parser.py`

```python
"""Tests for extracting file paths from plan artifacts."""
import pytest
from plan_parser import extract_files_from_plan


def test_extracts_simple_create_paths():
    plan = """
    ## Files
    - Create: `src/auth/login.py`
    - Modify: `src/auth/middleware.py`
    - Test: `tests/test_login.py`
    """
    files = extract_files_from_plan(plan)
    assert "src/auth/login.py" in files
    assert "src/auth/middleware.py" in files
    assert "tests/test_login.py" in files


def test_extracts_paths_from_code_blocks():
    plan = """
    ### Task 1
    Create `factory/new_module.py` with this content:
    ```python
    def foo(): pass
    ```
    """
    files = extract_files_from_plan(plan)
    assert "factory/new_module.py" in files


def test_ignores_non_file_backticks():
    plan = """
    Call the `FactoryDB.get_job` method.
    Use the `foo` variable.
    Create: `src/real_file.py`
    """
    files = extract_files_from_plan(plan)
    assert "src/real_file.py" in files
    assert "FactoryDB.get_job" not in files
    assert "foo" not in files


def test_deduplicates_paths():
    plan = """
    - Create: `src/foo.py`
    - Modify: `src/foo.py`
    - Also update `src/foo.py`
    """
    files = extract_files_from_plan(plan)
    assert files.count("src/foo.py") == 1


def test_extracts_paths_with_subdirs():
    plan = """
    - `mcp-server/src/tools/new_tool.ts`
    - `migrations/005_new_migration.sql`
    """
    files = extract_files_from_plan(plan)
    assert "mcp-server/src/tools/new_tool.ts" in files
    assert "migrations/005_new_migration.sql" in files


def test_empty_plan_returns_empty_list():
    assert extract_files_from_plan("") == []
    assert extract_files_from_plan("Just prose with no code.") == []


def test_strips_trailing_punctuation():
    plan = """
    Edit `src/foo.py`, then run tests.
    """
    files = extract_files_from_plan(plan)
    assert "src/foo.py" in files
    assert "src/foo.py," not in files
```

**Step 2: Run tests to verify failures**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_plan_parser.py -v`
Expected: FAIL — module doesn't exist

**Step 3: Implement plan_parser.py**

Create: `factory/plan_parser.py`

```python
"""Parse factory plan artifacts to extract file paths.

The planning phase produces a plan doc that lists files to create/modify.
We extract those paths so the file registry can lock them before implementation.
"""

from __future__ import annotations

import re


# File paths have:
# - Extension we recognize
# - Optional directory path with forward slashes
# - No spaces
# - Not a method call (no dot followed by identifier in the middle unless it's the extension)
FILE_EXTENSIONS = {
    "py", "ts", "tsx", "js", "jsx", "sql", "md", "yaml", "yml",
    "json", "toml", "sh", "go", "rs", "java", "c", "cpp", "h",
    "css", "html", "txt", "env",
}


def extract_files_from_plan(plan_text: str) -> list[str]:
    """Extract file paths from a plan document.

    Looks for:
    - Paths in backticks: `src/foo.py`
    - Paths after "Create:", "Modify:", "Test:", "Edit:", "Update:"
    - Paths with recognized file extensions

    Returns deduplicated list of paths.
    """
    if not plan_text:
        return []

    found: set[str] = set()

    # Pattern 1: paths in backticks
    backtick_paths = re.findall(r"`([^`\n]+?)`", plan_text)
    for candidate in backtick_paths:
        cleaned = candidate.strip().rstrip(",.;:")
        if _looks_like_file_path(cleaned):
            found.add(cleaned)

    # Pattern 2: paths after action keywords (Create:, Modify:, etc.)
    # This catches paths that aren't in backticks
    action_pattern = r"(?:Create|Modify|Test|Edit|Update|Add):\s*`?([^\s`\n]+)`?"
    for match in re.finditer(action_pattern, plan_text, re.IGNORECASE):
        candidate = match.group(1).strip().rstrip(",.;:")
        if _looks_like_file_path(candidate):
            found.add(candidate)

    return sorted(found)


def _looks_like_file_path(text: str) -> bool:
    """Check if text looks like a file path (vs. a code reference or identifier)."""
    if not text or " " in text:
        return False

    # Must have an extension we recognize
    if "." not in text:
        return False

    ext = text.rsplit(".", 1)[-1].lower()
    if ext not in FILE_EXTENSIONS:
        return False

    # Reject things that look like method calls: ClassName.method_name
    # Real paths have / or are just filename.ext
    # Method calls have no / and the part before the dot is CamelCase or has underscores
    if "/" not in text:
        # It's just filename.ext — check it's not a method call
        name_part = text.rsplit(".", 1)[0]
        if name_part and name_part[0].isupper() and "_" not in name_part:
            # Looks like ClassName.ext — probably a method ref if ext isn't real
            # But ClassName.py is valid for a file
            pass  # Allow it if ext is real

    return True
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_plan_parser.py -v`
Expected: All 7 tests PASS

**Step 5: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/plan_parser.py factory/tests/test_plan_parser.py
git commit -m "feat: add plan artifact parser for file path extraction"
```

---

## Task 5: Integrate File Registry into Orchestrator

**Files:**
- Modify: `factory/orchestrator.py` (add imports, modify `_run_planning` and add `_run_waiting`)

**Step 1: Add imports**

At the top of `factory/orchestrator.py` (near existing imports around line 20):

```python
from file_registry import FileRegistry
from plan_parser import extract_files_from_plan
```

**Step 2: Modify `_run_planning` to acquire locks after planning**

Find `_run_planning` (line 148-225) and modify the success path (lines 209-222) to:

1. Extract files from the plan
2. Attempt to acquire locks
3. If conflicts → transition to WAITING, store blocking_job_id in metadata
4. If clear → create branch and transition to IMPLEMENTING (existing behavior)

Replace the success block in `_run_planning` with:

```python
        if result.success:
            # Extract files the plan will modify
            plan_files = extract_files_from_plan(result.stdout)

            # Attempt to acquire file locks
            registry = FileRegistry(self.db)
            lock_result = registry.acquire_locks(
                job_id=job.id,
                project_id=job.project_id,
                file_paths=plan_files,
                dev_id=job.submitted_by,
            )

            if not lock_result.success:
                # File conflicts — go to WAITING
                blocking_job_id = lock_result.conflicts[0]["blocking_job_id"] if lock_result.conflicts else None
                logger.info(
                    "Job %s has %d file conflicts — transitioning to WAITING",
                    job.id[:8], len(lock_result.conflicts),
                )
                notify_desktop(
                    "DevBrain Factory",
                    f"Job waiting on file locks: {job.title}",
                )
                self.db.store_artifact(
                    job_id=job.id,
                    phase="planning",
                    artifact_type="lock_conflicts",
                    content=json.dumps(lock_result.conflicts, indent=2),
                )
                # Update blocked_by_job_id via direct SQL (transition doesn't support it)
                with self.db._conn() as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE devbrain.factory_jobs SET blocked_by_job_id = %s WHERE id = %s",
                        (blocking_job_id, job.id),
                    )
                    conn.commit()
                return self.db.transition(
                    job.id, JobStatus.WAITING,
                    metadata={"lock_conflicts": lock_result.conflicts},
                )

            # No conflicts — create branch and proceed
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

            return self.db.transition(job.id, JobStatus.IMPLEMENTING, branch_name=branch)
        else:
            return self.db.transition(job.id, JobStatus.FAILED,
                                      metadata={"failure": f"Planning failed: {result.stderr[:500]}"})
```

**Step 3: Add `_run_waiting` method**

After `_run_planning` (before `_run_implementation`), add:

```python
    # ─── Waiting Phase ─────────────────────────────────────────────────────

    def _run_waiting(self, job: FactoryJob) -> FactoryJob:
        """Waiting phase: job is blocked by file lock conflicts.

        Poll the registry to see if the blocking job has released its locks.
        If the blocking job is now in a terminal state AND its locks are released,
        retry the lock acquisition.
        """
        import time

        registry = FileRegistry(self.db)

        # Get the files this job needs (from planning artifact)
        artifacts = self.db.get_artifacts(job.id, phase="planning")
        plan_artifact = next(
            (a for a in artifacts if a["artifact_type"] == "plan_doc"),
            None,
        )
        if not plan_artifact:
            logger.warning("WAITING job %s has no plan artifact — failing", job.id[:8])
            return self.db.transition(
                job.id, JobStatus.FAILED,
                metadata={"failure": "No plan artifact for WAITING job"},
            )

        plan_files = extract_files_from_plan(plan_artifact["content"])

        # Poll loop: check every 30 seconds, max 1 hour
        max_wait_seconds = 3600
        poll_interval = 30
        waited = 0

        while waited < max_wait_seconds:
            # Clean up expired locks (safety net for crashed jobs)
            registry.cleanup_expired_locks()

            # Try to acquire locks
            lock_result = registry.acquire_locks(
                job_id=job.id,
                project_id=job.project_id,
                file_paths=plan_files,
                dev_id=job.submitted_by,
            )

            if lock_result.success:
                # Clear blocked_by_job_id
                with self.db._conn() as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE devbrain.factory_jobs SET blocked_by_job_id = NULL WHERE id = %s",
                        (job.id,),
                    )
                    conn.commit()

                # Create branch and move to IMPLEMENTING
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

                logger.info("Job %s unblocked after %ds wait", job.id[:8], waited)
                notify_desktop(
                    "DevBrain Factory",
                    f"Job unblocked: {job.title}",
                )
                return self.db.transition(
                    job.id, JobStatus.IMPLEMENTING, branch_name=branch,
                )

            time.sleep(poll_interval)
            waited += poll_interval

        # Timeout — fail the job
        logger.warning("Job %s waited %ds without clearing — failing", job.id[:8], waited)
        return self.db.transition(
            job.id, JobStatus.FAILED,
            metadata={"failure": f"Waited {waited}s for file locks without clearing"},
        )
```

**Step 4: Add WAITING to the pipeline loop**

In `run_job` (around line 82-96), add a handler for `WAITING`:

```python
            if job.status == JobStatus.QUEUED:
                job = self._run_planning(job)
            elif job.status == JobStatus.WAITING:
                job = self._run_waiting(job)
            elif job.status == JobStatus.IMPLEMENTING:
                job = self._run_implementation(job)
            # ...existing handlers
```

Also add `WAITING` to the terminal check (line 75-81) — actually NO, WAITING is not terminal, it stays in the loop. The loop should continue processing WAITING.

**Step 5: Run tests to verify no regressions**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/ -v`
Expected: All previous tests pass

**Step 6: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/orchestrator.py
git commit -m "feat: integrate file registry into orchestrator with WAITING state"
```

---

## Task 6: Cleanup Agent Releases File Locks

**Files:**
- Modify: `factory/cleanup_agent.py`

**Step 1: Write the failing test**

Add to `factory/tests/test_cleanup_agent.py`:

```python
def test_post_cleanup_releases_file_locks(db, agent):
    """Post-run cleanup releases all file locks held by the job."""
    from file_registry import FileRegistry

    registry = FileRegistry(db)

    # Create a completed job with locks
    job_id = db.create_job(project_slug="devbrain", title="Lock release test", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.FAILED)

    job = db.get_job(job_id)
    registry.acquire_locks(
        job_id=job.id,
        project_id=job.project_id,
        file_paths=["src/test_cleanup_lock_a.py", "src/test_cleanup_lock_b.py"],
        dev_id="alice",
    )

    # Verify locks exist
    assert len(registry.get_job_locks(job.id)) == 2

    # Run cleanup
    agent.run_post_cleanup(job.id)

    # Verify locks released
    assert len(registry.get_job_locks(job.id)) == 0


def test_cleanup_also_runs_expired_lock_cleanup(db, agent):
    """Cleanup agent triggers expired lock cleanup as part of post-run."""
    from file_registry import FileRegistry

    registry = FileRegistry(db)

    # Create a terminal job
    job_id = db.create_job(project_slug="devbrain", title="Expired cleanup", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.FAILED)

    # Run cleanup — should complete without error
    report = agent.run_post_cleanup(job_id)
    assert report["outcome"] == "failed"
```

**Step 2: Modify cleanup_agent.py**

Add import at the top:

```python
from file_registry import FileRegistry
```

In `run_post_cleanup`, after `self.db.archive_job(job_id)` (around line 119), add:

```python
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
```

**Step 3: Run tests**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_cleanup_agent.py -v`
Expected: All tests pass including new ones

**Step 4: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/cleanup_agent.py factory/tests/test_cleanup_agent.py
git commit -m "feat: cleanup agent releases file locks and cleans expired ones"
```

---

## Task 7: MCP Tool Updates — Expose File Locks and Dev Identity

**Files:**
- Modify: `mcp-server/src/index.ts`

**Step 1: Update `factory_plan` to capture submitted_by**

Modify the `factory_plan` tool (around line 401-451) to accept and store `submitted_by`:

Add to the schema:
```typescript
  submitted_by: z.string().optional().describe('Dev identifier (SSH user) who submitted this job'),
```

Update the INSERT to include `submitted_by`:

```typescript
    const result = await query<{ id: string }>(
      `INSERT INTO devbrain.factory_jobs
          (project_id, title, spec, status, priority, current_phase, assigned_cli, max_retries, submitted_by)
       VALUES ($1, $2, $3, 'queued', $4, 'queued', $5, 5, $6)
       RETURNING id`,
      [projectId, title, spec, priority, assigned_cli ?? 'claude', submitted_by ?? process.env.USER ?? null],
    )
```

**Step 2: Add `factory_file_locks` tool**

After the `factory_cleanup` tool, add:

```typescript
// ─── Tool: factory_file_locks ───────────────────────────────────────────────

server.tool(
  'factory_file_locks',
  'Show currently locked files in the factory. Use to debug why a job is WAITING, or see what other devs are working on.',
  {
    project: z.string().optional().describe('Project slug (defaults to DEVBRAIN_PROJECT)'),
  },
  async ({ project }) => {
    const slug = project ?? DEFAULT_PROJECT
    if (!slug) {
      return { content: [{ type: 'text', text: 'No project specified.' }] }
    }

    const projectId = await resolveProjectId(slug)
    if (!projectId) {
      return { content: [{ type: 'text', text: `Project "${slug}" not found.` }] }
    }

    const result = await query(
      `SELECT fl.file_path, fl.dev_id, fl.locked_at, fl.expires_at,
              j.title as job_title, j.status as job_status, j.id as job_id
       FROM devbrain.file_locks fl
       JOIN devbrain.factory_jobs j ON fl.job_id = j.id
       WHERE fl.project_id = $1 AND fl.expires_at > now()
       ORDER BY fl.locked_at ASC`,
      [projectId],
    )

    if (result.rows.length === 0) {
      return { content: [{ type: 'text', text: `No file locks active for project "${slug}".` }] }
    }

    return {
      content: [{
        type: 'text',
        text: JSON.stringify({
          project: slug,
          active_locks: result.rows.length,
          locks: result.rows,
        }, null, 2),
      }],
    }
  },
)
```

**Step 3: Update `get_project_context` to include lock info**

In the `get_project_context` tool, add a query for active locks and include in response:

```typescript
      query(`SELECT COUNT(*) as count FROM devbrain.file_locks
             WHERE project_id = $1 AND expires_at > now()`, [projectId]),
```

Add to the context:
```typescript
      active_file_locks: Number(lockCount.rows[0]?.count ?? 0),
```

**Step 4: Update `factory_status` to show WAITING jobs**

The current query at line 483 filters out `approved, rejected, deployed, failed`. That already includes WAITING correctly. But the response should clarify WAITING status. No changes needed — WAITING will show up as a status value.

**Step 5: Build**

Run: `cd /Users/patrickkelly/devbrain/mcp-server && npm run build`
Expected: Clean compile

**Step 6: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add mcp-server/src/index.ts
git commit -m "feat: add factory_file_locks tool and submitted_by to factory_plan"
```

---

## Task 8: End-to-End Integration Test

**Files:**
- Create: `factory/tests/test_multi_dev_integration.py`

**Step 1: Write the integration test**

```python
"""End-to-end integration test for multi-dev file locking."""
import pytest
from state_machine import FactoryDB, JobStatus
from file_registry import FileRegistry
from cleanup_agent import CleanupAgent

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


def test_two_jobs_independent_files_both_proceed(db):
    """Two jobs with no file overlap both acquire locks successfully."""
    registry = FileRegistry(db)

    job_a = db.get_job(db.create_job(project_slug="devbrain", title="Independent A", spec="Test"))
    job_b = db.get_job(db.create_job(project_slug="devbrain", title="Independent B", spec="Test"))

    result_a = registry.acquire_locks(
        job_a.id, job_a.project_id,
        ["src/feat_a/one.py", "src/feat_a/two.py"],
        dev_id="alice",
    )
    result_b = registry.acquire_locks(
        job_b.id, job_b.project_id,
        ["src/feat_b/one.py", "src/feat_b/two.py"],
        dev_id="bob",
    )

    assert result_a.success is True
    assert result_b.success is True


def test_two_jobs_shared_file_second_blocks(db):
    """Two jobs touching the same file: first wins, second blocks."""
    registry = FileRegistry(db)

    job_a = db.get_job(db.create_job(project_slug="devbrain", title="Shared A", spec="Test"))
    job_b = db.get_job(db.create_job(project_slug="devbrain", title="Shared B", spec="Test"))

    result_a = registry.acquire_locks(
        job_a.id, job_a.project_id,
        ["src/shared/common.py"],
        dev_id="alice",
    )
    result_b = registry.acquire_locks(
        job_b.id, job_b.project_id,
        ["src/shared/common.py"],
        dev_id="bob",
    )

    assert result_a.success is True
    assert result_b.success is False
    assert result_b.conflicts[0]["file_path"] == "src/shared/common.py"
    assert result_b.conflicts[0]["blocking_job_id"] == job_a.id


def test_cleanup_releases_then_blocked_job_proceeds(db):
    """After first job's cleanup, blocked job can acquire locks."""
    registry = FileRegistry(db)
    cleanup = CleanupAgent(db)

    # Job A acquires lock on shared file
    job_a_id = db.create_job(project_slug="devbrain", title="Release test A", spec="Test")
    db.transition(job_a_id, JobStatus.PLANNING)
    job_a = db.get_job(job_a_id)
    registry.acquire_locks(
        job_a.id, job_a.project_id,
        ["src/release_test_shared.py"],
        dev_id="alice",
    )

    # Job B tries same file — blocked
    job_b_id = db.create_job(project_slug="devbrain", title="Release test B", spec="Test")
    job_b = db.get_job(job_b_id)
    result = registry.acquire_locks(
        job_b.id, job_b.project_id,
        ["src/release_test_shared.py"],
        dev_id="bob",
    )
    assert result.success is False

    # Job A completes and cleanup runs
    db.transition(job_a_id, JobStatus.FAILED)
    cleanup.run_post_cleanup(job_a_id)

    # Job B can now acquire
    result = registry.acquire_locks(
        job_b.id, job_b.project_id,
        ["src/release_test_shared.py"],
        dev_id="bob",
    )
    assert result.success is True

    # Cleanup for job B
    db.transition(job_b_id, JobStatus.PLANNING)
    db.transition(job_b_id, JobStatus.FAILED)
    cleanup.run_post_cleanup(job_b_id)
```

**Step 2: Run the integration tests**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_multi_dev_integration.py -v`
Expected: 3 PASS

**Step 3: Run ALL tests**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/ -v`
Expected: All tests pass

**Step 4: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/tests/test_multi_dev_integration.py
git commit -m "test: add end-to-end multi-dev file locking integration tests"
```

---

## Summary

| Task | What | Key Files |
|------|------|-----------|
| 1 | DB migration: file_locks + submitted_by + blocked_by_job_id | `migrations/004_file_registry.sql` |
| 2 | WAITING state in state machine | `factory/state_machine.py` |
| 3 | FileRegistry module with lock CRUD | `factory/file_registry.py` |
| 4 | Plan artifact file extraction | `factory/plan_parser.py` |
| 5 | Orchestrator integration (WAITING phase + lock acquisition) | `factory/orchestrator.py` |
| 6 | Cleanup agent releases locks | `factory/cleanup_agent.py` |
| 7 | MCP tools: factory_file_locks, submitted_by | `mcp-server/src/index.ts` |
| 8 | End-to-end integration tests | `factory/tests/test_multi_dev_integration.py` |

**Dependencies:**
- Task 1 (DB) must run first
- Task 2 (state machine) depends on 1
- Tasks 3, 4 can run in parallel (both depend on 1 only)
- Task 5 depends on 2, 3, 4
- Task 6 depends on 3
- Task 7 depends on 1 (can run anytime after)
- Task 8 depends on 3, 5, 6

**Out of scope for this plan (future work):**
- Mac Studio deployment (Docker Compose setup, SSH config, tmux sessions)
- Per-dev MCP server isolation
- CI/CD integration for approved jobs → production
- Dev notification routing (currently all notifications go to desktop of whichever machine runs the factory)
