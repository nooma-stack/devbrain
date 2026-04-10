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
