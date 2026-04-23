"""Recent events panel — feed of factory activity."""

from __future__ import annotations

from datetime import datetime, timezone
from rich.markup import escape as _escape_markup
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
        log = RichLog(id="events-log", highlight=True, markup=True, wrap=False, max_lines=100)
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
            # job_title (user-supplied via factory_plan) and summary
            # (LLM-generated artifact text) can contain literal square
            # brackets — e.g. "Fix [E1234]" or "Added [type] annotations"
            # — which Rich parses as markup tags now that markup=True is
            # set. Unescaped, stray tags either raise MarkupError (line
            # silently dropped) or corrupt the rendered output. Escape
            # them; leave the dashboard-controlled wrapping markup
            # ([dim], [{color}]) intact since those are hardcoded here.
            job_title = _escape_markup(event["job_title"][:30])
            summary = _escape_markup(event["summary"])
            line = f"[dim]{time_str}[/dim] [{color}]{job_short}[/{color}] {job_title} — {summary}"
            log.write(line)
