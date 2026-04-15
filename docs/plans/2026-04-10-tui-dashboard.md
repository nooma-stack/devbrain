# DevBrain Factory TUI Dashboard Implementation Plan

> **Historical planning document.** Absolute paths and test commands in
> this doc reflect the dev environment at authorship time. For current
> install and test procedures see [INSTALL.md](../../INSTALL.md).
>
> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a real-time terminal dashboard (`devbrain dashboard`) that shows active factory jobs, recent events, file locks, and recently completed jobs — updating automatically every few seconds. Runs in any tmux pane so devs can watch their factory work progress alongside their coding session.

**Architecture:** A [Textual](https://textual.textualize.io/) TUI app with four panels: active jobs table, recent events log, file locks table, and recent completed jobs list. Data is polled from the existing DevBrain DB tables every 2 seconds (factory_jobs, factory_artifacts, file_locks, factory_cleanup_reports) — no new tables or schema changes. Selecting a job opens a modal with full details (plan, artifacts, cleanup reports). Registered as a `devbrain dashboard` subcommand that runs the app.

**Tech Stack:** Python, Textual (TUI framework), psycopg2, click (existing CLI), pytest + textual.pilot (for tests)

---

## Task 1: Install Textual and create dashboard skeleton

**Files:**
- Modify: `requirements.txt`
- Create: `factory/dashboard/__init__.py`
- Create: `factory/dashboard/app.py`
- Create: `factory/dashboard/data.py`
- Test: `factory/tests/test_dashboard_data.py`

**Step 1: Install Textual**

Run: `cd /Users/patrickkelly/devbrain && .venv/bin/pip install textual`
Expected: Textual installed (latest stable, e.g. 0.86+)

Add to `requirements.txt`:
```
textual>=0.80
```

**Step 2: Create the dashboard package**

Create `factory/dashboard/__init__.py` (empty).

**Step 3: Write the data layer tests first**

Create `factory/tests/test_dashboard_data.py`:

```python
"""Tests for dashboard data queries."""
import pytest
from state_machine import FactoryDB, JobStatus
from file_registry import FileRegistry
from dashboard.data import DashboardData

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture(autouse=True)
def cleanup(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM devbrain.file_locks WHERE file_path LIKE 'src/dashtest_%'")
        cur.execute("SELECT id FROM devbrain.factory_jobs WHERE title LIKE '%dashtest_%'")
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            cur.execute("DELETE FROM devbrain.factory_cleanup_reports WHERE job_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM devbrain.factory_artifacts WHERE job_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM devbrain.factory_jobs WHERE id = ANY(%s)", (ids,))
        conn.commit()


@pytest.fixture
def data(db):
    return DashboardData(db)


def test_get_active_jobs_returns_only_active(db, data):
    """Active jobs exclude terminal states and archived."""
    active_id = db.create_job(project_slug="devbrain", title="dashtest_active", spec="Test")
    db.transition(active_id, JobStatus.PLANNING)

    failed_id = db.create_job(project_slug="devbrain", title="dashtest_failed", spec="Test")
    db.transition(failed_id, JobStatus.PLANNING)
    db.transition(failed_id, JobStatus.FAILED)

    active_jobs = data.get_active_jobs()
    titles = [j["title"] for j in active_jobs]
    assert "dashtest_active" in titles
    assert "dashtest_failed" not in titles


def test_get_active_jobs_excludes_archived(db, data):
    archived_id = db.create_job(project_slug="devbrain", title="dashtest_archived", spec="Test")
    db.transition(archived_id, JobStatus.PLANNING)
    db.transition(archived_id, JobStatus.FAILED)
    db.archive_job(archived_id)

    active = data.get_active_jobs()
    assert not any(j["title"] == "dashtest_archived" for j in active)


def test_get_recent_events_returns_artifact_events(db, data):
    job_id = db.create_job(project_slug="devbrain", title="dashtest_events", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    db.store_artifact(job_id, "planning", "plan_doc", "Test plan content")
    db.store_artifact(job_id, "reviewing", "arch_review", "Review content", blocking_count=2)

    events = data.get_recent_events(limit=20)
    titles = [e["summary"] for e in events]
    assert any("plan_doc" in s or "arch_review" in s for s in titles)


def test_get_active_file_locks(db, data):
    job_id = db.create_job(project_slug="devbrain", title="dashtest_locks", spec="Test")
    registry = FileRegistry(db)
    registry.acquire_locks(
        job_id,
        db.get_job(job_id).project_id,
        ["src/dashtest_a.py", "src/dashtest_b.py"],
        dev_id="alice",
    )

    locks = data.get_active_locks()
    paths = [l["file_path"] for l in locks]
    assert "src/dashtest_a.py" in paths
    assert "src/dashtest_b.py" in paths


def test_get_recent_completed_jobs(db, data):
    job_id = db.create_job(project_slug="devbrain", title="dashtest_completed", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.IMPLEMENTING)
    db.transition(job_id, JobStatus.REVIEWING)
    db.transition(job_id, JobStatus.QA)
    db.transition(job_id, JobStatus.READY_FOR_APPROVAL)
    db.transition(job_id, JobStatus.APPROVED)

    completed = data.get_recent_completed()
    assert any(j["title"] == "dashtest_completed" for j in completed)


def test_get_job_details(db, data):
    """get_job_details returns full job info for the detail modal."""
    job_id = db.create_job(project_slug="devbrain", title="dashtest_details", spec="Test spec")
    db.transition(job_id, JobStatus.PLANNING)
    db.store_artifact(job_id, "planning", "plan_doc", "Detailed plan")

    details = data.get_job_details(job_id)
    assert details["title"] == "dashtest_details"
    assert details["status"] == "planning"
    assert "spec" in details
    assert len(details["artifacts"]) >= 1
```

**Step 4: Run the tests to verify they fail**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_dashboard_data.py -v`
Expected: FAIL — `dashboard.data` module doesn't exist

**Step 5: Implement the data layer**

Create `factory/dashboard/data.py`:

```python
"""Dashboard data queries — pulls factory state from the DevBrain DB."""

from __future__ import annotations

from state_machine import FactoryDB


class DashboardData:
    """Read-only data access for the dashboard.

    All queries are snapshot reads against the DevBrain DB. The dashboard
    polls this class on a tick to refresh its views.
    """

    def __init__(self, db: FactoryDB):
        self.db = db

    def get_active_jobs(self, project: str | None = None, limit: int = 20) -> list[dict]:
        """Jobs in flight: not terminal, not archived."""
        conditions = [
            "j.status NOT IN ('approved', 'rejected', 'deployed', 'failed')",
            "j.archived_at IS NULL",
        ]
        params: list = []
        if project:
            conditions.append("p.slug = %s")
            params.append(project)

        where = " AND ".join(conditions)
        params.append(limit)

        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT j.id, j.title, j.status, j.current_phase, j.submitted_by,
                       j.error_count, j.max_retries, j.branch_name,
                       j.updated_at, j.blocked_by_job_id, p.slug
                FROM devbrain.factory_jobs j
                JOIN devbrain.projects p ON j.project_id = p.id
                WHERE {where}
                ORDER BY j.updated_at DESC
                LIMIT %s
                """,
                params,
            )
            return [
                {
                    "id": str(r[0]),
                    "title": r[1],
                    "status": r[2],
                    "current_phase": r[3],
                    "submitted_by": r[4],
                    "error_count": r[5],
                    "max_retries": r[6],
                    "branch_name": r[7],
                    "updated_at": r[8],
                    "blocked_by_job_id": str(r[9]) if r[9] else None,
                    "project": r[10],
                }
                for r in cur.fetchall()
            ]

    def get_recent_events(
        self,
        project: str | None = None,
        limit: int = 30,
        since_minutes: int = 60,
    ) -> list[dict]:
        """Recent factory activity — artifact creations + cleanup reports."""
        conditions = [f"a.created_at > now() - interval '{int(since_minutes)} minutes'"]
        params: list = []
        if project:
            conditions.append("p.slug = %s")
            params.append(project)

        where = " AND ".join(conditions)
        params.append(limit)

        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT a.job_id, j.title, a.phase, a.artifact_type,
                       a.findings_count, a.blocking_count,
                       a.created_at, p.slug, j.status
                FROM devbrain.factory_artifacts a
                JOIN devbrain.factory_jobs j ON a.job_id = j.id
                JOIN devbrain.projects p ON j.project_id = p.id
                WHERE {where}
                ORDER BY a.created_at DESC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()

        events = []
        for r in rows:
            blocking = r[5] or 0
            findings = r[4] or 0
            if r[3] in ("arch_review", "security_review"):
                summary = f"{r[3]}: {blocking} blocking, {findings - blocking} other findings"
            elif r[3] == "plan_doc":
                summary = "planning complete"
            elif r[3] == "impl_output":
                summary = "implementation complete"
            elif r[3] == "qa_report":
                summary = "QA complete"
            elif r[3] == "diff":
                summary = "diff captured"
            elif r[3] == "lock_conflicts":
                summary = "BLOCKED on file lock conflicts"
            else:
                summary = r[3]

            events.append({
                "job_id": str(r[0]),
                "job_title": r[1],
                "phase": r[2],
                "artifact_type": r[3],
                "summary": summary,
                "blocking_count": blocking,
                "timestamp": r[6],
                "project": r[7],
                "job_status": r[8],
            })
        return events

    def get_active_locks(self, project: str | None = None) -> list[dict]:
        """Currently held file locks."""
        conditions = ["fl.expires_at > now()"]
        params: list = []
        if project:
            conditions.append("p.slug = %s")
            params.append(project)

        where = " AND ".join(conditions)

        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT fl.file_path, fl.dev_id, fl.locked_at, fl.expires_at,
                       j.id, j.title, j.status, p.slug
                FROM devbrain.file_locks fl
                JOIN devbrain.factory_jobs j ON fl.job_id = j.id
                JOIN devbrain.projects p ON j.project_id = p.id
                WHERE {where}
                ORDER BY fl.locked_at ASC
                """,
                params,
            )
            return [
                {
                    "file_path": r[0],
                    "dev_id": r[1],
                    "locked_at": r[2],
                    "expires_at": r[3],
                    "job_id": str(r[4]),
                    "job_title": r[5],
                    "job_status": r[6],
                    "project": r[7],
                }
                for r in cur.fetchall()
            ]

    def get_recent_completed(
        self,
        project: str | None = None,
        hours: int = 24,
        limit: int = 15,
    ) -> list[dict]:
        """Jobs that reached a terminal state in the last N hours."""
        conditions = [
            "j.status IN ('approved', 'rejected', 'deployed', 'failed')",
            f"j.updated_at > now() - interval '{int(hours)} hours'",
        ]
        params: list = []
        if project:
            conditions.append("p.slug = %s")
            params.append(project)

        where = " AND ".join(conditions)
        params.append(limit)

        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT j.id, j.title, j.status, j.submitted_by,
                       j.updated_at, j.error_count, p.slug
                FROM devbrain.factory_jobs j
                JOIN devbrain.projects p ON j.project_id = p.id
                WHERE {where}
                ORDER BY j.updated_at DESC
                LIMIT %s
                """,
                params,
            )
            return [
                {
                    "id": str(r[0]),
                    "title": r[1],
                    "status": r[2],
                    "submitted_by": r[3],
                    "updated_at": r[4],
                    "error_count": r[5],
                    "project": r[6],
                }
                for r in cur.fetchall()
            ]

    def get_job_details(self, job_id: str) -> dict | None:
        """Full details for a single job — used by the detail modal."""
        job = self.db.get_job(job_id)
        if not job:
            return None

        artifacts = self.db.get_artifacts(job_id)
        reports = self.db.get_cleanup_reports(job_id)

        return {
            "id": job.id,
            "title": job.title,
            "status": job.status.value,
            "current_phase": job.current_phase,
            "submitted_by": job.submitted_by,
            "branch_name": job.branch_name,
            "error_count": job.error_count,
            "max_retries": job.max_retries,
            "spec": job.spec or "",
            "created_at": str(job.created_at),
            "updated_at": str(job.updated_at),
            "blocked_by_job_id": job.blocked_by_job_id,
            "blocked_resolution": job.blocked_resolution,
            "metadata": job.metadata,
            "artifacts": [
                {
                    "phase": a["phase"],
                    "artifact_type": a["artifact_type"],
                    "findings_count": a["findings_count"],
                    "blocking_count": a["blocking_count"],
                    "created_at": a["created_at"],
                    "content_preview": (a["content"] or "")[:500],
                }
                for a in artifacts
            ],
            "cleanup_reports": [
                {
                    "report_type": r["report_type"],
                    "outcome": r["outcome"],
                    "summary": r["summary"][:500],
                    "created_at": r["created_at"],
                }
                for r in reports
            ],
        }
```

**Step 6: Run the tests**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_dashboard_data.py -v`
Expected: All tests PASS.

**Step 7: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/dashboard/__init__.py factory/dashboard/data.py factory/tests/test_dashboard_data.py requirements.txt
git commit -m "feat: add dashboard data layer with read-only DevBrain queries"
```

---

## Task 2: Dashboard App — Skeleton with Header and Footer

**Files:**
- Create: `factory/dashboard/app.py`
- Create: `factory/dashboard/widgets/__init__.py`
- Test: `factory/tests/test_dashboard_app.py`

**Step 1: Write the failing test**

Create `factory/tests/test_dashboard_app.py`:

```python
"""Tests for the DevBrain dashboard Textual app."""
import pytest
from dashboard.app import DashboardApp


@pytest.mark.asyncio
async def test_dashboard_mounts():
    """Dashboard app can be mounted and has the expected title."""
    app = DashboardApp()
    async with app.run_test() as pilot:
        assert "DevBrain Factory Dashboard" in app.title or "Dashboard" in app.title


@pytest.mark.asyncio
async def test_dashboard_has_quit_binding():
    """Pressing 'q' exits the app."""
    app = DashboardApp()
    async with app.run_test() as pilot:
        await pilot.press("q")
        assert app.is_running is False or app.return_value is not None
```

Add to `requirements.txt`: `pytest-asyncio>=0.23`

Run: `cd /Users/patrickkelly/devbrain && .venv/bin/pip install pytest-asyncio`

**Step 2: Run the test to verify failure**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_dashboard_app.py -v`
Expected: FAIL — `dashboard.app` doesn't exist

**Step 3: Implement the skeleton app**

Create `factory/dashboard/widgets/__init__.py` (empty).

Create `factory/dashboard/app.py`:

```python
"""DevBrain Factory Dashboard — Textual TUI app.

Real-time view of factory jobs, events, file locks, and recent completions.
Polls DevBrain DB every REFRESH_INTERVAL seconds.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Header, Footer, Label, Static

from state_machine import FactoryDB
from dashboard.data import DashboardData

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"
REFRESH_INTERVAL = 2.0  # seconds


class DashboardApp(App):
    """DevBrain factory dashboard."""

    TITLE = "DevBrain Factory Dashboard"
    CSS = """
    Screen {
        background: $surface;
    }
    #main {
        layout: vertical;
    }
    .panel {
        border: round $accent;
        padding: 0 1;
        margin: 0 1 1 1;
    }
    .panel-title {
        text-style: bold;
        color: $accent;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh now"),
    ]

    current_project: reactive[str | None] = reactive(None)

    def __init__(self, project: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self.current_project = project
        self.db = FactoryDB(DATABASE_URL)
        self.data = DashboardData(self.db)

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="main"):
            yield Static("Loading...", id="loading")
        yield Footer()

    def on_mount(self) -> None:
        """Called when the app mounts. Start the refresh timer."""
        self.set_interval(REFRESH_INTERVAL, self.refresh_data)
        self.refresh_data()

    def refresh_data(self) -> None:
        """Poll the DB and update the dashboard. Subclasses will override."""
        try:
            jobs = self.data.get_active_jobs(project=self.current_project)
            loading = self.query_one("#loading", Static)
            loading.update(f"Active jobs: {len(jobs)}")
        except Exception as e:
            loading = self.query_one("#loading", Static)
            loading.update(f"Error: {e}")

    def action_refresh(self) -> None:
        """Manual refresh."""
        self.refresh_data()


def main():
    """CLI entry point."""
    app = DashboardApp()
    app.run()


if __name__ == "__main__":
    main()
```

**Step 4: Run tests to verify they pass**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_dashboard_app.py -v`
Expected: Both tests PASS.

**Step 5: Manual smoke test**

Run: `cd /Users/patrickkelly/devbrain && .venv/bin/python -m factory.dashboard.app`
Expected: Textual app launches, shows header with title, shows "Active jobs: N", footer with quit binding. Press `q` to exit.

**Step 6: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/dashboard/app.py factory/dashboard/widgets/__init__.py factory/tests/test_dashboard_app.py requirements.txt
git commit -m "feat: add dashboard Textual app skeleton with auto-refresh"
```

---

## Task 3: Active Jobs Panel

**Files:**
- Create: `factory/dashboard/widgets/jobs_panel.py`
- Modify: `factory/dashboard/app.py`

**Step 1: Create the ActiveJobsPanel widget**

Create `factory/dashboard/widgets/jobs_panel.py`:

```python
"""Active jobs panel — table of running factory jobs."""

from __future__ import annotations

from datetime import datetime, timezone
from textual.widgets import DataTable, Static
from textual.containers import Vertical


STATUS_ICONS = {
    "queued":             "⏳",
    "planning":           "📝",
    "blocked":            "🔒",
    "implementing":       "🟢",
    "reviewing":          "👁",
    "qa":                 "🧪",
    "fix_loop":           "🔄",
    "ready_for_approval": "✅",
    "approved":           "👍",
    "failed":             "❌",
    "rejected":           "🚫",
    "deployed":           "🚀",
}


def _phase_progress(status: str) -> tuple[int, int]:
    """Return (current_phase_idx, total_phases)."""
    phases = ["queued", "planning", "implementing", "reviewing", "qa", "ready_for_approval"]
    try:
        idx = phases.index(status) + 1
    except ValueError:
        idx = 0
    return idx, len(phases)


def _format_age(updated_at) -> str:
    """Format a timestamp as a human-readable age."""
    if updated_at is None:
        return "?"
    try:
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - updated_at
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m"
        if seconds < 86400:
            return f"{seconds // 3600}h"
        return f"{seconds // 86400}d"
    except Exception:
        return "?"


class ActiveJobsPanel(Vertical):
    """Panel showing currently active factory jobs."""

    DEFAULT_CSS = """
    ActiveJobsPanel {
        height: auto;
        max-height: 20;
    }
    ActiveJobsPanel DataTable {
        height: auto;
    }
    """

    def compose(self):
        yield Static("━━━ Active Jobs ━━━", classes="panel-title")
        table = DataTable(id="jobs-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("", "ID", "Title", "Phase", "Progress", "Dev", "Age")
        yield table

    def update_jobs(self, jobs: list[dict]) -> None:
        table = self.query_one(DataTable)
        table.clear()

        if not jobs:
            table.add_row("", "", "No active jobs", "", "", "", "")
            return

        for job in jobs:
            icon = STATUS_ICONS.get(job["status"], "•")
            job_id_short = job["id"][:8]
            title = job["title"][:40] + ("…" if len(job["title"]) > 40 else "")
            phase = job["status"]
            current_idx, total = _phase_progress(job["status"])
            progress = f"{current_idx}/{total}"
            dev = job.get("submitted_by") or "—"
            age = _format_age(job.get("updated_at"))

            # Show retry count for fix_loop or error states
            if job.get("error_count", 0) > 0:
                phase = f"{phase} ({job['error_count']}/{job['max_retries']})"

            table.add_row(icon, job_id_short, title, phase, progress, dev, age, key=job["id"])
```

**Step 2: Integrate the panel into the app**

Modify `factory/dashboard/app.py`. Update the `compose` method and `refresh_data`:

```python
    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="main"):
            yield ActiveJobsPanel(id="jobs-panel", classes="panel")
        yield Footer()

    def refresh_data(self) -> None:
        try:
            jobs = self.data.get_active_jobs(project=self.current_project)
            panel = self.query_one("#jobs-panel", ActiveJobsPanel)
            panel.update_jobs(jobs)
        except Exception as e:
            # Surface errors via notify
            self.notify(f"Refresh error: {e}", severity="error")
```

Add import at top: `from dashboard.widgets.jobs_panel import ActiveJobsPanel`

**Step 3: Add a test for the panel**

Append to `factory/tests/test_dashboard_app.py`:

```python
@pytest.mark.asyncio
async def test_dashboard_shows_active_jobs_panel():
    """Dashboard renders the active jobs panel."""
    from dashboard.widgets.jobs_panel import ActiveJobsPanel
    app = DashboardApp()
    async with app.run_test() as pilot:
        panel = app.query_one(ActiveJobsPanel)
        assert panel is not None


@pytest.mark.asyncio
async def test_jobs_panel_renders_rows(db):
    """Panel displays job data."""
    from state_machine import JobStatus
    from dashboard.widgets.jobs_panel import ActiveJobsPanel

    # Create a test job
    job_id = db.create_job(project_slug="devbrain", title="dashtest_panel_render", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)

    app = DashboardApp()
    async with app.run_test() as pilot:
        await pilot.pause(0.1)  # let refresh happen
        panel = app.query_one(ActiveJobsPanel)
        from textual.widgets import DataTable
        table = panel.query_one(DataTable)
        # Table should have at least one row (could have more from other tests)
        assert table.row_count >= 1
```

Add a `db` fixture import at the top of the test file:

```python
import pytest
from state_machine import FactoryDB
from dashboard.app import DashboardApp

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"

@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)

@pytest.fixture(autouse=True)
def cleanup(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM devbrain.factory_jobs WHERE title LIKE '%dashtest_%'")
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            cur.execute("DELETE FROM devbrain.factory_cleanup_reports WHERE job_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM devbrain.factory_artifacts WHERE job_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM devbrain.factory_jobs WHERE id = ANY(%s)", (ids,))
        conn.commit()
```

**Step 4: Run tests**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_dashboard_app.py -v`
Expected: All tests PASS.

**Step 5: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/dashboard/widgets/jobs_panel.py factory/dashboard/app.py factory/tests/test_dashboard_app.py
git commit -m "feat: add active jobs panel to dashboard"
```

---

## Task 4: Recent Events Feed + File Locks Panel

**Files:**
- Create: `factory/dashboard/widgets/events_panel.py`
- Create: `factory/dashboard/widgets/locks_panel.py`
- Modify: `factory/dashboard/app.py`

**Step 1: Create the events panel**

Create `factory/dashboard/widgets/events_panel.py`:

```python
"""Recent events panel — feed of factory activity."""

from __future__ import annotations

from datetime import datetime, timezone
from textual.widgets import RichLog, Static
from textual.containers import Vertical


EVENT_COLORS = {
    "plan_doc":        "cyan",
    "impl_output":     "green",
    "arch_review":     "yellow",
    "security_review": "yellow",
    "qa_report":       "magenta",
    "lock_conflicts":  "red",
    "diff":            "blue",
    "fix_output":      "orange3",
}


def _format_time(ts) -> str:
    if ts is None:
        return "?"
    try:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone().strftime("%H:%M:%S")
    except Exception:
        return "?"


class RecentEventsPanel(Vertical):
    """Scrolling log of recent factory activity."""

    DEFAULT_CSS = """
    RecentEventsPanel {
        height: auto;
        max-height: 15;
    }
    RecentEventsPanel RichLog {
        height: auto;
        max-height: 12;
    }
    """

    def compose(self):
        yield Static("━━━ Recent Events ━━━", classes="panel-title")
        log = RichLog(id="events-log", highlight=True, wrap=False, max_lines=100)
        yield log

    def update_events(self, events: list[dict]) -> None:
        log = self.query_one(RichLog)
        log.clear()

        if not events:
            log.write("[dim]No recent events[/dim]")
            return

        # Events come newest-first; display oldest-first (append order)
        for event in reversed(events):
            color = EVENT_COLORS.get(event["artifact_type"], "white")
            time_str = _format_time(event["timestamp"])
            job_short = event["job_id"][:8]
            job_title = event["job_title"][:30]
            summary = event["summary"]
            line = f"[dim]{time_str}[/dim] [{color}]{job_short}[/{color}] {job_title} — {summary}"
            log.write(line)
```

**Step 2: Create the locks panel**

Create `factory/dashboard/widgets/locks_panel.py`:

```python
"""File locks panel — currently held locks."""

from __future__ import annotations

from textual.widgets import DataTable, Static
from textual.containers import Vertical


class FileLocksPanel(Vertical):
    """Panel showing active file locks."""

    DEFAULT_CSS = """
    FileLocksPanel {
        height: auto;
        max-height: 12;
    }
    """

    def compose(self):
        yield Static("━━━ File Locks ━━━", classes="panel-title")
        table = DataTable(id="locks-table", zebra_stripes=True)
        table.add_columns("File", "Held by Job", "Dev", "Status")
        yield table

    def update_locks(self, locks: list[dict]) -> None:
        table = self.query_one(DataTable)
        table.clear()

        if not locks:
            table.add_row("No active file locks", "", "", "")
            return

        for lock in locks:
            file_path = lock["file_path"]
            if len(file_path) > 60:
                file_path = "…" + file_path[-58:]
            job_title = lock["job_title"][:25]
            job_short = lock["job_id"][:8]
            dev = lock.get("dev_id") or "—"
            status = lock["job_status"]
            table.add_row(
                f"🔒 {file_path}",
                f"{job_short} {job_title}",
                dev,
                status,
            )
```

**Step 3: Integrate both panels into the app**

Update `factory/dashboard/app.py`:

```python
from dashboard.widgets.jobs_panel import ActiveJobsPanel
from dashboard.widgets.events_panel import RecentEventsPanel
from dashboard.widgets.locks_panel import FileLocksPanel

# In compose:
    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="main"):
            yield ActiveJobsPanel(id="jobs-panel", classes="panel")
            yield RecentEventsPanel(id="events-panel", classes="panel")
            yield FileLocksPanel(id="locks-panel", classes="panel")
        yield Footer()

# In refresh_data:
    def refresh_data(self) -> None:
        try:
            jobs = self.data.get_active_jobs(project=self.current_project)
            events = self.data.get_recent_events(project=self.current_project)
            locks = self.data.get_active_locks(project=self.current_project)

            self.query_one("#jobs-panel", ActiveJobsPanel).update_jobs(jobs)
            self.query_one("#events-panel", RecentEventsPanel).update_events(events)
            self.query_one("#locks-panel", FileLocksPanel).update_locks(locks)
        except Exception as e:
            self.notify(f"Refresh error: {e}", severity="error")
```

**Step 4: Add tests**

Append to `factory/tests/test_dashboard_app.py`:

```python
@pytest.mark.asyncio
async def test_dashboard_shows_events_panel():
    from dashboard.widgets.events_panel import RecentEventsPanel
    app = DashboardApp()
    async with app.run_test():
        panel = app.query_one(RecentEventsPanel)
        assert panel is not None


@pytest.mark.asyncio
async def test_dashboard_shows_locks_panel():
    from dashboard.widgets.locks_panel import FileLocksPanel
    app = DashboardApp()
    async with app.run_test():
        panel = app.query_one(FileLocksPanel)
        assert panel is not None
```

**Step 5: Run tests**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_dashboard_app.py -v`
Expected: All PASS.

**Step 6: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/dashboard/widgets/events_panel.py factory/dashboard/widgets/locks_panel.py factory/dashboard/app.py factory/tests/test_dashboard_app.py
git commit -m "feat: add recent events feed and file locks panels to dashboard"
```

---

## Task 5: Recent Completed Panel + Job Detail Modal

**Files:**
- Create: `factory/dashboard/widgets/completed_panel.py`
- Create: `factory/dashboard/widgets/job_detail.py`
- Modify: `factory/dashboard/app.py`

**Step 1: Create the completed panel**

Create `factory/dashboard/widgets/completed_panel.py`:

```python
"""Recent completed jobs panel."""

from __future__ import annotations

from textual.widgets import DataTable, Static
from textual.containers import Vertical


STATUS_EMOJI = {
    "approved": "✅",
    "deployed": "🚀",
    "rejected": "🚫",
    "failed":   "❌",
}


class RecentCompletedPanel(Vertical):
    """Panel showing recently completed jobs (last 24h)."""

    DEFAULT_CSS = """
    RecentCompletedPanel {
        height: auto;
        max-height: 12;
    }
    """

    def compose(self):
        yield Static("━━━ Recently Completed (24h) ━━━", classes="panel-title")
        table = DataTable(id="completed-table", zebra_stripes=True)
        table.add_columns("", "ID", "Title", "Status", "Dev", "Retries")
        yield table

    def update_completed(self, completed: list[dict]) -> None:
        table = self.query_one(DataTable)
        table.clear()

        if not completed:
            table.add_row("", "", "No recently completed jobs", "", "", "")
            return

        for job in completed:
            emoji = STATUS_EMOJI.get(job["status"], "•")
            jid = job["id"][:8]
            title = job["title"][:40]
            status = job["status"]
            dev = job.get("submitted_by") or "—"
            retries = f"{job.get('error_count', 0)}"
            table.add_row(emoji, jid, title, status, dev, retries, key=job["id"])
```

**Step 2: Create the job detail modal**

Create `factory/dashboard/widgets/job_detail.py`:

```python
"""Job detail modal screen — shows full job info when selected."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Container, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static


class JobDetailScreen(ModalScreen):
    """Modal dialog showing full job details."""

    DEFAULT_CSS = """
    JobDetailScreen {
        align: center middle;
    }
    #dialog {
        width: 100;
        max-width: 90%;
        height: 40;
        max-height: 90%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #title {
        text-style: bold;
        color: $accent;
    }
    #content {
        height: 1fr;
        overflow-y: scroll;
    }
    """

    BINDINGS = [
        ("escape,q", "dismiss", "Close"),
    ]

    def __init__(self, job_details: dict, **kwargs):
        super().__init__(**kwargs)
        self.job_details = job_details

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            j = self.job_details
            yield Label(f"📋 {j['title']}", id="title")
            yield Label(
                f"ID: {j['id'][:8]} | Status: {j['status']} | "
                f"Dev: {j.get('submitted_by') or '—'} | "
                f"Retries: {j.get('error_count', 0)}/{j.get('max_retries', 0)}"
            )
            if j.get("branch_name"):
                yield Label(f"Branch: {j['branch_name']}")

            with VerticalScroll(id="content"):
                yield Static("[bold]Spec:[/bold]")
                yield Static(j.get("spec", "(no spec)")[:1000])

                if j.get("artifacts"):
                    yield Static("\n[bold]Artifacts:[/bold]")
                    for a in j["artifacts"]:
                        yield Static(
                            f"  • {a['phase']}/{a['artifact_type']} — "
                            f"findings: {a['findings_count']}, "
                            f"blocking: {a['blocking_count']} "
                            f"({a['created_at'][:19]})"
                        )

                if j.get("cleanup_reports"):
                    yield Static("\n[bold]Cleanup Reports:[/bold]")
                    for r in j["cleanup_reports"]:
                        yield Static(
                            f"  • [{r['report_type']}] {r['outcome']} ({r['created_at'][:19]})"
                        )
                        yield Static(f"    {r['summary'][:300]}")

            yield Label("[dim]Press Esc or q to close[/dim]")

    def action_dismiss(self) -> None:
        self.app.pop_screen()
```

**Step 3: Integrate into app with row selection**

Update `factory/dashboard/app.py`:

```python
from dashboard.widgets.completed_panel import RecentCompletedPanel
from dashboard.widgets.job_detail import JobDetailScreen
from textual.widgets import DataTable
from textual import on

# In compose, add completed panel after locks panel
    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="main"):
            yield ActiveJobsPanel(id="jobs-panel", classes="panel")
            yield RecentEventsPanel(id="events-panel", classes="panel")
            yield FileLocksPanel(id="locks-panel", classes="panel")
            yield RecentCompletedPanel(id="completed-panel", classes="panel")
        yield Footer()

# In refresh_data, add completed
    def refresh_data(self) -> None:
        try:
            jobs = self.data.get_active_jobs(project=self.current_project)
            events = self.data.get_recent_events(project=self.current_project)
            locks = self.data.get_active_locks(project=self.current_project)
            completed = self.data.get_recent_completed(project=self.current_project)

            self.query_one("#jobs-panel", ActiveJobsPanel).update_jobs(jobs)
            self.query_one("#events-panel", RecentEventsPanel).update_events(events)
            self.query_one("#locks-panel", FileLocksPanel).update_locks(locks)
            self.query_one("#completed-panel", RecentCompletedPanel).update_completed(completed)
        except Exception as e:
            self.notify(f"Refresh error: {e}", severity="error")

# Add event handler for row selection
    @on(DataTable.RowSelected)
    def handle_row_selected(self, event: DataTable.RowSelected) -> None:
        """Open the job detail modal when a row is selected."""
        row_key = event.row_key.value if event.row_key else None
        if not row_key:
            return
        # row_key is the job_id
        details = self.data.get_job_details(row_key)
        if details:
            self.push_screen(JobDetailScreen(details))
```

**Step 4: Run tests**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_dashboard_app.py -v`
Expected: All PASS.

**Step 5: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/dashboard/widgets/completed_panel.py factory/dashboard/widgets/job_detail.py factory/dashboard/app.py
git commit -m "feat: add recent completed panel and job detail modal to dashboard"
```

---

## Task 6: CLI Integration — `devbrain dashboard` command

**Files:**
- Modify: `factory/cli.py`

**Step 1: Add the dashboard command**

In `factory/cli.py`, add after the `watch` command (or after `telegram-discover`):

```python
@cli.command(name="dashboard")
@click.option("--project", default=None, help="Filter by project slug")
def dashboard(project):
    """Launch the DevBrain factory dashboard (TUI)."""
    try:
        from dashboard.app import DashboardApp
    except ImportError as e:
        click.echo(
            f"Error: Textual not installed. Run: pip install textual\n{e}",
            err=True,
        )
        sys.exit(1)

    app = DashboardApp(project=project)
    app.run()
```

**Step 2: Add a test**

Append to `factory/tests/test_cli.py`:

```python
def test_dashboard_command_exists(runner):
    """devbrain dashboard is a registered command."""
    result = runner.invoke(cli, ["dashboard", "--help"])
    assert result.exit_code == 0
    assert "dashboard" in result.output.lower()
```

**Step 3: Smoke test**

Run: `/Users/patrickkelly/devbrain/bin/devbrain dashboard --help`
Expected: Shows help for the dashboard command.

(You can also run `/Users/patrickkelly/devbrain/bin/devbrain dashboard` to actually launch it — press `q` to quit.)

**Step 4: Run tests**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_cli.py -v`
Expected: All PASS.

**Step 5: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/cli.py factory/tests/test_cli.py
git commit -m "feat: add devbrain dashboard CLI command"
```

---

## Task 7: Documentation

**Files:**
- Create: `docs/dashboard.md`

**Step 1: Write the docs**

Create `docs/dashboard.md`:

```markdown
# DevBrain Factory Dashboard

A real-time terminal UI for watching factory jobs as they progress.

## Launch

```bash
devbrain dashboard                     # All projects
devbrain dashboard --project myproject # Filter to one project
```

Runs in any terminal (including over SSH, inside tmux). Uses [Textual](https://textual.textualize.io/).

## Layout

Four panels, top to bottom:

1. **Active Jobs** — Jobs currently in flight. Shows icon, short ID, title, current phase, progress fraction, submitting dev, and last-update age. Select a row (arrow keys + Enter) to open the detail modal.

2. **Recent Events** — Scrolling log of factory activity from the last hour. Each line shows timestamp, job ID, job title, and a summary of what happened (e.g. "arch review: 2 blocking, 1 warning").

3. **File Locks** — All currently-held file locks across the team. Shows file path, owning job, dev, and job status. Useful for spotting why your job might be blocked.

4. **Recently Completed** — Jobs that reached a terminal state in the last 24 hours. Shows status emoji, ID, title, final status, dev, and retry count.

## Keyboard shortcuts

- `q` — quit
- `r` — refresh now (auto-refresh runs every 2 seconds anyway)
- `↑ ↓` — navigate rows in a table
- `Enter` — open job detail modal for selected row
- `Esc` — close the detail modal

## Job detail modal

Pressing Enter on a job row opens a modal with:
- Full title, status, branch, retry count
- Spec (the original feature request)
- List of artifacts with phase, findings count, blocking count
- Cleanup reports (post-run, recovery, blocked investigations) with summaries

## How it works

Polls the DevBrain DB every 2 seconds. No new schema — all data comes from existing tables:
- `devbrain.factory_jobs` for active and completed jobs
- `devbrain.factory_artifacts` for recent events
- `devbrain.file_locks` for lock info
- `devbrain.factory_cleanup_reports` for investigation reports in the detail modal

## Tips

- Run it in a dedicated tmux pane so you always have factory visibility while coding.
- Combine with `devbrain watch` (notification tail) for a full observability setup.
- If you're a dev working on a shared team, leave it open to spot conflicts before they block you.
```

**Step 2: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add docs/dashboard.md
git commit -m "docs: add dashboard usage guide"
```

---

## Summary

| Task | What | Depends on |
|------|------|-----------|
| 1 | Data layer (DashboardData queries) + tests | — |
| 2 | Dashboard app skeleton with auto-refresh | 1 |
| 3 | Active jobs panel | 2 |
| 4 | Recent events feed + file locks panels | 2 |
| 5 | Recent completed panel + job detail modal | 2 |
| 6 | `devbrain dashboard` CLI command | 2 |
| 7 | Documentation | 6 |

**Parallelization:**
- Task 1 first
- Task 2 second
- Tasks 3, 4, 5 can run in parallel after Task 2
- Tasks 6 and 7 after all widgets are done

**Design highlights:**

1. **Zero new schema** — all data comes from existing tables. No migration required.
2. **Read-only** — dashboard never writes to the DB, it's purely observational.
3. **Polling, not push** — simple 2-second interval. Can upgrade to `LISTEN/NOTIFY` later if needed.
4. **Four focused panels** — active jobs, events, locks, completed. Each ~10-15 lines of screen space.
5. **Job detail modal** — keyboard-driven drill-down to full context without losing the overview.
6. **Agent-agnostic** — it's a plain terminal app. Devs can run it alongside Claude, Codex, Gemini, vim, whatever.
7. **Graceful degradation** — if Textual isn't installed, the CLI command shows a helpful error.

**Out of scope (follow-ups):**
- PostgreSQL LISTEN/NOTIFY for instant updates
- Advanced filtering (by dev, by status, search)
- Mouse support / click handlers
- Custom themes / color preferences
- Multi-project side-by-side view
- Export view to text/JSON
