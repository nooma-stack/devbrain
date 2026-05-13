"""Tests for the cognify status panel (data layer + widget).

The data-layer method `get_cognify_pass_status` is exercised against a
mock DB so we can control timing edge cases without spinning up a real
cognify_run_log. The widget tests are pure rendering — no DB.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock


# ─── widget rendering ────────────────────────────────────────────────────────


def test_format_relative_seconds():
    from dashboard.widgets.cognify_panel import _format_relative
    now = datetime.now(timezone.utc)
    assert _format_relative(now - timedelta(seconds=5)) == "5s ago"
    assert _format_relative(now - timedelta(seconds=59)) == "59s ago"


def test_format_relative_minutes():
    from dashboard.widgets.cognify_panel import _format_relative
    now = datetime.now(timezone.utc)
    assert _format_relative(now - timedelta(minutes=2)) == "2m ago"


def test_format_relative_hours():
    from dashboard.widgets.cognify_panel import _format_relative
    now = datetime.now(timezone.utc)
    assert _format_relative(now - timedelta(hours=4)) == "4h ago"


def test_format_relative_days():
    from dashboard.widgets.cognify_panel import _format_relative
    now = datetime.now(timezone.utc)
    assert _format_relative(now - timedelta(days=3)) == "3d ago"


def test_format_relative_none():
    from dashboard.widgets.cognify_panel import _format_relative
    assert _format_relative(None) == "—"


def test_format_relative_naive_datetime_treated_as_utc():
    from dashboard.widgets.cognify_panel import _format_relative
    now_naive = datetime.utcnow() - timedelta(seconds=12)
    result = _format_relative(now_naive)
    # Don't pin the exact number (clock skew); just confirm it parses
    # as a recent past time, not raising on tz comparison.
    assert "s ago" in result or "m ago" in result


def test_format_interval():
    from dashboard.widgets.cognify_panel import _format_interval
    assert _format_interval(0) == "?"
    assert _format_interval(60) == "1m"
    assert _format_interval(3600) == "1h"
    assert _format_interval(86_400) == "1d"
    assert _format_interval(604_800) == "7d"


# ─── data layer state derivation ─────────────────────────────────────────────


class _MockCursor:
    """Lightweight stand-in for psycopg2 cursor — yields canned results
    from a list, one per execute() call."""

    def __init__(self, results):
        self._results = list(results)
        self._current = None

    def execute(self, query, params=None):
        self._current = self._results.pop(0) if self._results else None

    def fetchone(self):
        return self._current

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _MockConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _build_data_with_canned(results: list):
    """Return a DashboardData instance whose `db._conn()` yields a
    connection that produces `results` in order, one per execute()."""
    from dashboard.data import DashboardData

    db = MagicMock()
    cursor = _MockCursor(results)
    conn = _MockConn(cursor)
    db._conn.return_value = conn
    return DashboardData(db=db)


def test_state_never_run_when_no_log_rows():
    """All 5 passes return rows even when cognify_run_log is empty."""
    data = _build_data_with_canned([None] * 5)
    rows = data.get_cognify_pass_status()
    assert len(rows) == 5
    assert all(r["state"] == "never_run" for r in rows)
    assert {r["pass_name"] for r in rows} == {
        "extract", "edges", "decay", "strengthen", "gc"
    }


def test_state_running_when_started_no_complete():
    """started_at present, completed_at NULL → state='running'."""
    now = datetime.now(timezone.utc)
    running_row = (now - timedelta(seconds=10), None, None, None, None)
    data = _build_data_with_canned([running_row, None, None, None, None])
    rows = data.get_cognify_pass_status()
    extract = next(r for r in rows if r["pass_name"] == "extract")
    assert extract["state"] == "running"


def test_state_idle_when_recent_completion():
    """Completed within expected interval → state='idle'."""
    now = datetime.now(timezone.utc)
    # extract expected hourly; 5 minutes ago counts as idle
    idle_row = (
        now - timedelta(minutes=10),  # started_at
        now - timedelta(minutes=5),   # completed_at
        12,                            # rows_processed
        3,                             # llm_calls
        None,                          # error
    )
    data = _build_data_with_canned([idle_row, None, None, None, None])
    rows = data.get_cognify_pass_status()
    extract = next(r for r in rows if r["pass_name"] == "extract")
    assert extract["state"] == "idle"
    assert extract["last_rows_processed"] == 12
    assert extract["last_llm_calls"] == 3


def test_state_errored_when_error_set():
    """Most recent completed run has error → state='errored'."""
    now = datetime.now(timezone.utc)
    err_row = (
        now - timedelta(minutes=10),
        now - timedelta(minutes=5),
        0,
        0,
        "ConnectionRefusedError: connect ECONNREFUSED 127.0.0.1:5432\nstack...",
    )
    data = _build_data_with_canned([err_row, None, None, None, None])
    rows = data.get_cognify_pass_status()
    extract = next(r for r in rows if r["pass_name"] == "extract")
    assert extract["state"] == "errored"
    assert extract["last_error"].startswith("ConnectionRefusedError")
    # Error is truncated to 200 chars
    assert len(extract["last_error"]) <= 200


def test_state_down_when_well_past_expected_interval():
    """No completion within 3× expected interval → state='down'."""
    now = datetime.now(timezone.utc)
    # extract expected hourly; 5 hours ago is past 3× = down
    stale_row = (
        now - timedelta(hours=5, minutes=1),
        now - timedelta(hours=5),
        5,
        1,
        None,  # success but old
    )
    data = _build_data_with_canned([stale_row, None, None, None, None])
    rows = data.get_cognify_pass_status()
    extract = next(r for r in rows if r["pass_name"] == "extract")
    assert extract["state"] == "down"


def test_state_idle_at_2x_interval_not_yet_down():
    """A run 2× expected_interval old is still 'idle', not 'down' (down
    threshold is 3×). This documents the boundary."""
    now = datetime.now(timezone.utc)
    # extract expected hourly; 2 hours ago is past 1× but not past 3×
    row = (
        now - timedelta(hours=2, minutes=1),
        now - timedelta(hours=2),
        5,
        1,
        None,
    )
    data = _build_data_with_canned([row, None, None, None, None])
    rows = data.get_cognify_pass_status()
    extract = next(r for r in rows if r["pass_name"] == "extract")
    assert extract["state"] == "idle"


def test_widget_renders_all_states(monkeypatch):
    """Sanity: CognifyPanel.update_passes handles every defined state
    without raising. Uses a stub DataTable so we don't need a running
    textual app."""
    from dashboard.widgets.cognify_panel import CognifyPanel

    # Build a fake panel — bypass textual's compose lifecycle.
    panel = CognifyPanel.__new__(CognifyPanel)

    rows_logged: list[tuple] = []

    class _FakeTable:
        def clear(self):
            rows_logged.clear()

        def add_row(self, *cols):
            rows_logged.append(cols)

    fake_table = _FakeTable()
    monkeypatch.setattr(panel, "query_one", lambda _cls: fake_table)

    now = datetime.now(timezone.utc)
    panel.update_passes([
        {"pass_name": "extract",    "state": "running",   "last_completed": None,
         "last_rows_processed": 0,  "last_llm_calls": 0, "last_error": None,
         "expected_interval_s": 3600},
        {"pass_name": "edges",      "state": "idle",      "last_completed": now - timedelta(minutes=30),
         "last_rows_processed": 5,  "last_llm_calls": 2, "last_error": None,
         "expected_interval_s": 21_600},
        {"pass_name": "decay",      "state": "errored",   "last_completed": now - timedelta(hours=1),
         "last_rows_processed": 0,  "last_llm_calls": 0, "last_error": "Boom\nfull stack here",
         "expected_interval_s": 3600},
        {"pass_name": "strengthen", "state": "down",      "last_completed": now - timedelta(days=5),
         "last_rows_processed": 0,  "last_llm_calls": 0, "last_error": None,
         "expected_interval_s": 86_400},
        {"pass_name": "gc",         "state": "never_run", "last_completed": None,
         "last_rows_processed": 0,  "last_llm_calls": 0, "last_error": None,
         "expected_interval_s": 604_800},
    ])

    assert len(rows_logged) == 5
    states_rendered = [row[1] for row in rows_logged]  # state column
    assert "running" in states_rendered[0]
    assert "idle" in states_rendered[1]
    assert "errored" in states_rendered[2]
    assert "down" in states_rendered[3]
    assert "never" in states_rendered[4]

    # The error note for `errored` is the first line of the error message,
    # truncated to 50 chars.
    assert "Boom" in rows_logged[2][5]
    assert "full stack here" not in rows_logged[2][5]
