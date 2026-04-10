"""DevBrain Factory Dashboard — Textual TUI app.

Real-time view of factory jobs, events, file locks, and recent completions.
Polls DevBrain DB every REFRESH_INTERVAL seconds.
"""

from __future__ import annotations

from textual import on
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Header, Footer, Label, Static, DataTable

from state_machine import FactoryDB
from dashboard.data import DashboardData
from dashboard.widgets.jobs_panel import ActiveJobsPanel
from dashboard.widgets.events_panel import RecentEventsPanel
from dashboard.widgets.locks_panel import FileLocksPanel
from dashboard.widgets.completed_panel import RecentCompletedPanel
from dashboard.widgets.job_detail import JobDetailScreen

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
            yield ActiveJobsPanel(id="jobs-panel", classes="panel")
            yield RecentEventsPanel(id="events-panel", classes="panel")
            yield FileLocksPanel(id="locks-panel", classes="panel")
            yield RecentCompletedPanel(id="completed-panel", classes="panel")
        yield Footer()

    def on_mount(self) -> None:
        """Called when the app mounts. Start the refresh timer."""
        self.set_interval(REFRESH_INTERVAL, self.refresh_data)
        self.refresh_data()

    def refresh_data(self) -> None:
        """Poll the DB and update all panels."""
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

    def action_refresh(self) -> None:
        """Manual refresh."""
        self.refresh_data()

    @on(DataTable.RowSelected)
    def handle_row_selected(self, event: DataTable.RowSelected) -> None:
        """Open the job detail modal when a row is selected."""
        row_key = event.row_key.value if event.row_key else None
        if not row_key:
            return
        details = self.data.get_job_details(row_key)
        if details:
            self.push_screen(JobDetailScreen(details))


def main():
    """CLI entry point."""
    app = DashboardApp()
    app.run()


if __name__ == "__main__":
    main()
