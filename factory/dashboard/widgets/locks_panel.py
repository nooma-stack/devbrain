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
