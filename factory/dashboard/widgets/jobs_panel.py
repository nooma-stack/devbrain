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


def _humanize_age(seconds: int) -> str:
    """Format an integer second count as a two-unit human-readable age.

    Bands: <1m → "Ns"; <1h → "Nm Ss"; <1d → "Nh Mm"; ≥1d → "Nd Hh".
    Negative inputs (clock skew) clamp to "0s".
    """
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def _format_age(updated_at) -> str:
    """Format a timestamp as a human-readable age."""
    if updated_at is None:
        return "?"
    try:
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - updated_at
        return _humanize_age(int(delta.total_seconds()))
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
