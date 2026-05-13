"""Cognify pass status panel.

Shows the 5 cognify passes (extract, edges, decay, strengthen, gc)
with their current state (running / idle / errored / down / never_run),
last completion time, and recent activity counters.

State semantics (see `DashboardData.get_cognify_pass_status`):

  ╔═══════════╤══════════════════════════════════════════════════════╗
  ║ running   │ Started_at exists, completed_at not yet recorded.    ║
  ║ idle      │ Most recent run completed cleanly within the         ║
  ║           │ pass's expected interval. Healthy.                   ║
  ║ errored   │ Most recent completed run has a non-null error.      ║
  ║ down      │ No run within 3× expected interval. Suggests the     ║
  ║           │ launchd job stopped firing (plist unloaded, etc.).   ║
  ║ never_run │ No cognify_run_log row for this pass yet.            ║
  ╚═══════════╧══════════════════════════════════════════════════════╝
"""

from __future__ import annotations

from datetime import datetime, timezone

from textual.containers import Vertical
from textual.widgets import DataTable, Static


_STATE_INDICATOR = {
    "running":   "🟢 running",
    "idle":      "⚪ idle",
    "errored":   "🔴 errored",
    "down":      "⛔ down",
    "never_run": "○ never run",
}


class CognifyPanel(Vertical):
    """Panel showing per-pass cognify run state + recent activity."""

    DEFAULT_CSS = """
    CognifyPanel {
        height: auto;
        max-height: 12;
    }
    """

    def compose(self):
        yield Static("━━━ Cognify ━━━", classes="panel-title")
        table = DataTable(id="cognify-table", zebra_stripes=True)
        table.add_columns("Pass", "State", "Last completed", "Rows", "LLM calls", "Note")
        yield table

    def update_passes(self, passes: list[dict]) -> None:
        """Re-populate the table from `DashboardData.get_cognify_pass_status`."""
        table = self.query_one(DataTable)
        table.clear()

        if not passes:
            table.add_row("No cognify passes registered", "", "", "", "", "")
            return

        for p in passes:
            pass_name = p["pass_name"]
            state = _STATE_INDICATOR.get(p["state"], p["state"])
            completed = _format_relative(p.get("last_completed"))
            rows_n = str(p.get("last_rows_processed") or 0)
            llm_n = str(p.get("last_llm_calls") or 0)

            # Note column: error message for `errored`, schedule hint
            # for `down`/`never_run`, blank otherwise.
            note = ""
            if p["state"] == "errored" and p.get("last_error"):
                note = (p["last_error"] or "").splitlines()[0][:50]
            elif p["state"] == "down":
                expected = p.get("expected_interval_s", 0)
                note = f"expected every {_format_interval(expected)}"
            elif p["state"] == "never_run":
                note = "no runs recorded"

            table.add_row(pass_name, state, completed, rows_n, llm_n, note)


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


def _format_interval(seconds: int) -> str:
    if seconds <= 0:
        return "?"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86_400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86_400}d"
