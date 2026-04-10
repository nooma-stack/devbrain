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
