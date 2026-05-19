"""Tests for the active-sessions panel (data layer + widget).

Issue #135 — surfaces (session_id, dev_id, cli, project_slug,
applied_at) for the dashboard "Recent Sessions" panel.

Data-layer tests use a real ephemeral DB (live psycopg2) when DATABASE_URL
is set, otherwise mock the DB cursor — mirrors the cognify panel test
structure.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import psycopg2
import psycopg2.extras
import pytest

# Make factory/ importable when pytest is invoked from the repo root
# (mirrors the harness used by the other dashboard tests).
_FACTORY = Path(__file__).resolve().parents[1]
if str(_FACTORY) not in sys.path:
    sys.path.insert(0, str(_FACTORY))


# ─── widget rendering ────────────────────────────────────────────────────────


def test_format_relative_seconds():
    from dashboard.widgets.sessions_panel import _format_relative
    now = datetime.now(timezone.utc)
    assert _format_relative(now - timedelta(seconds=5)) == "5s ago"
    assert _format_relative(now - timedelta(seconds=59)) == "59s ago"


def test_format_relative_minutes():
    from dashboard.widgets.sessions_panel import _format_relative
    now = datetime.now(timezone.utc)
    assert _format_relative(now - timedelta(minutes=2)) == "2m ago"


def test_format_relative_hours():
    from dashboard.widgets.sessions_panel import _format_relative
    now = datetime.now(timezone.utc)
    assert _format_relative(now - timedelta(hours=4)) == "4h ago"


def test_format_relative_days():
    from dashboard.widgets.sessions_panel import _format_relative
    now = datetime.now(timezone.utc)
    assert _format_relative(now - timedelta(days=3)) == "3d ago"


def test_format_relative_none():
    from dashboard.widgets.sessions_panel import _format_relative
    assert _format_relative(None) == "—"


def test_format_relative_iso_string():
    from dashboard.widgets.sessions_panel import _format_relative
    iso = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    # Just confirm it parses without raising; clock skew makes "==" flaky.
    assert "ago" in _format_relative(iso)


# ─── data-layer mock tests ──────────────────────────────────────────────────


def _build_data_with_mock_rows(rows: list[tuple]):
    """Construct a DashboardData instance whose DB returns *rows* for the
    sessions query."""
    from dashboard.data import DashboardData

    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = rows

    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__.return_value = mock_cursor

    mock_db = MagicMock()
    mock_db._conn.return_value.__enter__.return_value = mock_conn

    return DashboardData(mock_db), mock_cursor


def test_get_recent_sessions_maps_columns_to_dicts():
    now = datetime.now(timezone.utc)
    rows = [
        ("sess-A", "mike_courtney", "claude", now, "brightbot"),
        ("sess-B", None, None, now - timedelta(minutes=5), "devbrain"),
    ]
    data, _cur = _build_data_with_mock_rows(rows)
    result = data.get_recent_sessions(limit=2)

    assert len(result) == 2
    assert result[0] == {
        "session_id": "sess-A",
        "dev_id": "mike_courtney",
        "cli": "claude",
        "applied_at": now,
        "project_slug": "brightbot",
    }
    # NULL dev_id/cli passes through (panel renders as "—").
    assert result[1]["dev_id"] is None
    assert result[1]["cli"] is None


def test_get_recent_sessions_empty():
    data, _cur = _build_data_with_mock_rows([])
    assert data.get_recent_sessions() == []


def test_get_recent_sessions_passes_project_filter_to_sql():
    data, cur = _build_data_with_mock_rows([])
    data.get_recent_sessions(project="brightbot", limit=5)
    sql, params = cur.execute.call_args.args
    assert "p.slug = %s" in sql
    assert params == ["brightbot", 5]


def test_get_recent_sessions_no_filter_when_project_omitted():
    data, cur = _build_data_with_mock_rows([])
    data.get_recent_sessions(limit=3)
    sql, params = cur.execute.call_args.args
    assert "p.slug = %s" not in sql
    assert params == [3]


# ─── data-layer live-DB tests (skipped if no DATABASE_URL) ──────────────────


def _live_conn():
    """Connect to the DB DEVBRAIN config points at. Skips tests when the
    devbrain config / DATABASE_URL isn't reachable from the test env."""
    from config import DATABASE_URL  # noqa: PLC0415
    return psycopg2.connect(DATABASE_URL)


@pytest.fixture
def live_db():
    try:
        conn = _live_conn()
    except Exception as exc:
        pytest.skip(f"live DB unavailable: {exc}")
    psycopg2.extras.register_uuid()
    yield conn
    conn.close()


def test_get_recent_sessions_returns_inserted_row_live(live_db):
    """Insert a synthetic end_session_log row, query, confirm round-trip
    including dev_id + cli columns from migration 038."""
    from dashboard.data import DashboardData
    from state_machine import FactoryDB
    from config import DATABASE_URL  # noqa: PLC0415

    cur = live_db.cursor()
    # Need an existing project_id; use any.
    cur.execute("SELECT id, slug FROM devbrain.projects LIMIT 1")
    row = cur.fetchone()
    if row is None:
        pytest.skip("no projects in DB")
    project_id, project_slug = row

    sess_id = f"test-sessions-panel-{datetime.now(timezone.utc).timestamp()}"
    cur.execute(
        """
        INSERT INTO devbrain.end_session_log
            (session_id, payload_hash, project_id, result, dev_id, cli)
        VALUES (%s, %s, %s, %s::jsonb, %s, %s)
        """,
        (sess_id, "test-hash", project_id, '{"status":"applied"}',
         "test_dev", "claude"),
    )
    live_db.commit()
    try:
        data = DashboardData(FactoryDB(DATABASE_URL))
        rows = data.get_recent_sessions(limit=50)
        match = next(
            (r for r in rows if r["session_id"] == sess_id), None,
        )
        assert match is not None, "inserted row not surfaced by query"
        assert match["dev_id"] == "test_dev"
        assert match["cli"] == "claude"
        assert match["project_slug"] == project_slug
    finally:
        cur.execute(
            "DELETE FROM devbrain.end_session_log WHERE session_id = %s",
            (sess_id,),
        )
        live_db.commit()
