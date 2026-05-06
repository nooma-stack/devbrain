"""Tests for versioned re-extraction (migration 030 + reextract_cli --since-version).

Covers:
  - extraction_version is set to CURRENT_EXTRACTION_VERSION on new extract rows
  - extraction_version column exists on devbrain.memory (schema check)
  - reextract_cli --since-version selects sessions with old extraction_version
  - reextract_cli --since-version: old rows get archived_at set, new rows
    written with CURRENT_EXTRACTION_VERSION
  - reextract_cli --since-version dry_run: returns session count without mutating
  - _sessions_below_version: returns correct sessions
  - Mutual exclusion: since_version is exclusive with other flags
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from cognify.extract import (
    CURRENT_EXTRACTION_VERSION,
    _upsert_memory,
    extract_from_session,
)
from cognify.reextract_cli import _sessions_below_version, run_reextract


# ── Schema check (no LLM, no mock needed) ────────────────────────────────────


@pytest.mark.db
def test_extraction_version_column_exists(conn):
    """devbrain.memory has an extraction_version column (migration 030)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema = 'devbrain' "
            "  AND table_name = 'memory' "
            "  AND column_name = 'extraction_version'",
        )
        row = cur.fetchone()
    assert row is not None, "extraction_version column missing from devbrain.memory"
    col_name, data_type, default = row
    assert data_type in ("integer", "bigint"), f"unexpected type: {data_type}"
    assert "1" in str(default), f"expected default 1, got: {default}"


# ── CURRENT_EXTRACTION_VERSION sentinel ──────────────────────────────────────


def test_current_extraction_version_is_int():
    """CURRENT_EXTRACTION_VERSION must be a positive integer."""
    assert isinstance(CURRENT_EXTRACTION_VERSION, int)
    assert CURRENT_EXTRACTION_VERSION >= 1


# ── _upsert_memory sets extraction_version ────────────────────────────────────


@pytest.mark.db
def test_upsert_memory_sets_extraction_version(conn, project_factory):
    """New rows written by _upsert_memory carry CURRENT_EXTRACTION_VERSION."""
    project = project_factory("ver_upsert")
    session_id = str(uuid.uuid4())

    mid, was_new = _upsert_memory(
        conn,
        project_id=project["id"],
        kind="lesson",
        title="Versioned Lesson",
        content="Some content",
        session_id=session_id,
    )

    assert was_new is True
    with conn.cursor() as cur:
        cur.execute(
            "SELECT extraction_version FROM devbrain.memory WHERE id = %s",
            (mid,),
        )
        version = cur.fetchone()[0]
    assert version == CURRENT_EXTRACTION_VERSION


# ── extract_from_session integration ──────────────────────────────────────────


@pytest.mark.db
def test_extract_sets_extraction_version_on_new_rows(conn, project_factory):
    """extract_from_session writes new lesson rows with CURRENT_EXTRACTION_VERSION."""
    project = project_factory("ver_extract")
    session_id = str(uuid.uuid4())

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory "
            "(project_id, kind, title, content, provenance_id) "
            "VALUES (%s, 'pattern', 'raw chunk', 'content', %s::uuid)",
            (project["id"], session_id),
        )
    conn.commit()

    mock_response = {
        "lessons": [{"title": "Versioned Lesson", "content": "A great insight"}],
        "decisions": [],
        "_usage": {"input_tokens": 0, "output_tokens": 0,
                   "cache_read_tokens": 0, "cache_write_tokens": 0},
    }
    with patch("cognify.extract._llm_extract", return_value=mock_response):
        result = extract_from_session(conn, session_id, project["id"])

    assert result.lessons_created == 1

    # Verify the inserted row has the correct version.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT extraction_version FROM devbrain.memory "
            "WHERE project_id = %s "
            "  AND tier = 'lesson' "
            "  AND archived_at IS NULL "
            "  AND (applies_when->>'source_session') = %s",
            (project["id"], session_id),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == CURRENT_EXTRACTION_VERSION


# ── _sessions_below_version ───────────────────────────────────────────────────


@pytest.mark.db
def test_sessions_below_version_returns_stale_sessions(conn, project_factory):
    """_sessions_below_version returns sessions whose lesson rows have
    extraction_version < the given threshold."""
    project = project_factory("ver_below")
    session_old = str(uuid.uuid4())
    session_current = str(uuid.uuid4())

    # Insert an "old" lesson row (version 0) for session_old.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory "
            "(project_id, kind, title, content, tier, strength, applies_when, extraction_version) "
            "VALUES (%s, 'pattern', 'Old Lesson', 'content', 'lesson', 1.0, %s::jsonb, 0)",
            (project["id"], f'{{"source_session": "{session_old}"}}'),
        )
    conn.commit()

    # Insert a "current" lesson row (version = CURRENT_EXTRACTION_VERSION) for session_current.
    _upsert_memory(
        conn,
        project_id=project["id"],
        kind="lesson",
        title="Current Lesson",
        content="up-to-date",
        session_id=session_current,
    )

    # Query with threshold = CURRENT_EXTRACTION_VERSION.
    sessions = _sessions_below_version(conn, project["id"], CURRENT_EXTRACTION_VERSION)
    # Only the old session should be returned.
    assert session_old in sessions
    assert session_current not in sessions


@pytest.mark.db
def test_sessions_below_version_excludes_archived(conn, project_factory):
    """_sessions_below_version skips archived lesson rows."""
    project = project_factory("ver_archived")
    session_id = str(uuid.uuid4())

    # Insert a stale row but immediately archive it.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory "
            "(project_id, kind, title, content, tier, strength, applies_when, "
            " extraction_version, archived_at) "
            "VALUES (%s, 'pattern', 'Archived', 'body', 'lesson', 1.0, %s::jsonb, "
            "        0, NOW())",
            (project["id"], f'{{"source_session": "{session_id}"}}'),
        )
    conn.commit()

    sessions = _sessions_below_version(conn, project["id"], CURRENT_EXTRACTION_VERSION)
    assert session_id not in sessions


# ── run_reextract --since-version ─────────────────────────────────────────────


@pytest.mark.db
def test_reextract_since_version_archives_old_and_writes_new(conn, project_factory):
    """run_reextract --since-version archives stale rows and writes new ones
    with the current extraction_version."""
    project = project_factory("ver_reextract")
    session_id = str(uuid.uuid4())

    # Seed a raw chunk for the LLM to "process".
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory "
            "(project_id, kind, title, content, provenance_id) "
            "VALUES (%s, 'pattern', 'raw', 'raw content', %s::uuid)",
            (project["id"], session_id),
        )
    conn.commit()

    # Insert a stale lesson row (version 0) for this session.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory "
            "(project_id, kind, title, content, tier, strength, applies_when, extraction_version) "
            "VALUES (%s, 'pattern', 'Stale Lesson', 'old content', 'lesson', 1.0, "
            "        %s::jsonb, 0)",
            (project["id"], f'{{"source_session": "{session_id}"}}'),
        )
    conn.commit()

    # Mock the LLM to return a new lesson.
    mock_response = {
        "lessons": [{"title": "Fresh Lesson", "content": "Updated insight"}],
        "decisions": [],
        "_usage": {"input_tokens": 0, "output_tokens": 0,
                   "cache_read_tokens": 0, "cache_write_tokens": 0},
    }
    with patch("cognify.extract._llm_extract", return_value=mock_response):
        result = run_reextract(
            conn,
            project["id"],
            since_version=CURRENT_EXTRACTION_VERSION,
        )

    assert result["sessions_targeted"] == 1
    assert result["lessons_created"] == 1

    # Old row should be archived.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT archived_at FROM devbrain.memory WHERE title = 'Stale Lesson' "
            "AND project_id = %s",
            (project["id"],),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] is not None, "stale row should have been archived"

    # New row should exist with CURRENT_EXTRACTION_VERSION.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT extraction_version FROM devbrain.memory "
            "WHERE project_id = %s AND title = 'Fresh Lesson' AND archived_at IS NULL",
            (project["id"],),
        )
        new_row = cur.fetchone()
    assert new_row is not None
    assert new_row[0] == CURRENT_EXTRACTION_VERSION


@pytest.mark.db
def test_reextract_since_version_dry_run(conn, project_factory):
    """run_reextract --since-version dry_run returns session count without mutating."""
    project = project_factory("ver_dry")
    session_id = str(uuid.uuid4())

    # Insert a stale lesson row (version 0).
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory "
            "(project_id, kind, title, content, tier, strength, applies_when, extraction_version) "
            "VALUES (%s, 'pattern', 'Dry Stale', 'content', 'lesson', 1.0, %s::jsonb, 0)",
            (project["id"], f'{{"source_session": "{session_id}"}}'),
        )
    conn.commit()

    result = run_reextract(
        conn,
        project["id"],
        since_version=CURRENT_EXTRACTION_VERSION,
        dry_run=True,
    )

    assert result.get("dry_run") is True
    assert result.get("sessions_targeted") == 1

    # Verify the stale row was NOT archived.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT archived_at FROM devbrain.memory "
            "WHERE project_id = %s AND title = 'Dry Stale'",
            (project["id"],),
        )
        archived_at = cur.fetchone()[0]
    assert archived_at is None


@pytest.mark.db
def test_reextract_since_version_mutual_exclusion(conn, project_factory):
    """since_version is mutually exclusive with other reextract flags."""
    project = project_factory("ver_mutex")
    with pytest.raises(ValueError, match="Exactly one"):
        run_reextract(
            conn,
            project["id"],
            since_version=2,
            all_sessions=True,
        )


@pytest.mark.db
def test_reextract_since_version_no_stale_sessions(conn, project_factory):
    """run_reextract --since-version returns zero when no stale sessions exist."""
    project = project_factory("ver_empty")
    result = run_reextract(
        conn,
        project["id"],
        since_version=CURRENT_EXTRACTION_VERSION,
    )
    assert result["sessions_targeted"] == 0
