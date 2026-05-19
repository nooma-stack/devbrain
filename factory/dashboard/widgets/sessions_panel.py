"""Active Sessions panel (Issue #135).

Shows the most-recently-ended dev sessions, with dev_id + CLI
attribution sourced from end_session_log (populated by the MCP server's
end_session handler — see migration 038).

The "active" framing is approximate: this is "recently-ended" rather
than "currently connected" because the MCP server doesn't track open
sessions today. The 80% UX value lands either way — the panel shows
who's been working in which CLI lately, which is the operational
question this view answers.

Pre-038 rows (and Patrick's local non-SSH sessions) have NULL dev_id
and NULL cli; the panel renders those as "—" so the gap is visible
rather than silently inferred to a guessed default.
"""

from __future__ import annotations

from datetime import datetime, timezone

from textual.containers import Vertical
from textual.widgets import DataTable, Static


class SessionsPanel(Vertical):
    """Panel showing recently-ended dev sessions with attribution."""

    DEFAULT_CSS = """
    SessionsPanel {
        height: auto;
        max-height: 14;
    }
    """

    def compose(self):
        yield Static("━━━ Recent Sessions ━━━", classes="panel-title")
        table = DataTable(id="sessions-table", zebra_stripes=True)
        table.add_columns("Session", "Dev", "CLI", "Project", "Ended")
        yield table

    def update_sessions(self, sessions: list[dict]) -> None:
        """Re-populate the table from `DashboardData.get_recent_sessions`."""
        table = self.query_one(DataTable)
        table.clear()

        if not sessions:
            table.add_row("No recent end_session calls", "", "", "", "")
            return

        for s in sessions:
            session_short = (s["session_id"] or "")[:8] + "…"
            dev = s.get("dev_id") or "—"
            cli = s.get("cli") or "—"
            project = s.get("project_slug") or "—"
            ended = _format_relative(s.get("applied_at"))
            table.add_row(session_short, dev, cli, project, ended)


def _format_relative(ts) -> str:
    """Human-friendly age. e.g. '2m ago', '4h ago', '3d ago'."""
    if ts is None:
        return "—"
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    sec = int((now - ts).total_seconds())
    if sec < 60:
        return f"{sec}s ago"
    if sec < 3600:
        return f"{sec // 60}m ago"
    if sec < 86_400:
        return f"{sec // 3600}h ago"
    return f"{sec // 86_400}d ago"
