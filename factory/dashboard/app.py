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
