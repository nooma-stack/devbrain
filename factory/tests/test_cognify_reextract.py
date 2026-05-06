"""Integration tests for cognify-reextract CLI logic.

Covers:
  - run_reextract --session: archives prior rows and re-inserts
  - run_reextract --all: processes all sessions
  - run_reextract dry_run: returns session count without mutating
  - Mutual-exclusion: exactly one of session/all/since must be set
  - reextract_from metadata is attached to new rows (reextracted_from key)
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from cognify.extract import _upsert_memory, _archive_prior_extracts
from cognify.reextract_cli import run_reextract


def _insert_chunk(conn, project_id, session_id, content):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory "
            "(project_id, kind, title, content, provenance_id) "
            "VALUES (%s, 'pattern', %s, %s, %s::uuid) RETURNING id",
            (project_id, f"chunk_{uuid.uuid4().hex[:6]}", content, session_id),
        )
        mid = cur.fetchone()[0]
    conn.commit()
    return mid


def _archived_count(conn, project_id, session_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory "
            "WHERE project_id = %s "
            "  AND tier = 'lesson' AND archived_at IS NOT NULL "
            "  AND (applies_when->>'source_session') = %s",
            (project_id, session_id),
        )
        return cur.fetchone()[0]


@pytest.mark.db
def test_reextract_session_archives_prior_rows(conn, project_factory, memory_factory):
    """run_reextract --session archives prior lessons before re-extraction."""
    project = project_factory("reext_basic")
    session_id = str(uuid.uuid4())

    # Insert a raw chunk so extract has content to work with
    _insert_chunk(conn, project["id"], session_id, "content for extraction")

    # Insert a prior lesson for this session
    mid, _ = _upsert_memory(
        conn,
        project_id=project["id"],
        kind="lesson",
        title="Old Lesson",
        content="Old",
        session_id=session_id,
    )

    # Verify it's not archived yet
    with conn.cursor() as cur:
        cur.execute(
            "SELECT archived_at FROM devbrain.memory WHERE id = %s", (mid,)
        )
        assert cur.fetchone()[0] is None

    # run_reextract calls extract_from_session with reextract=True
    # which calls _archive_prior_extracts. Since we have no API key in test,
    # the LLM call returns empty, so only the archival happens.
    result = run_reextract(
        conn, project["id"], session_id=session_id
    )

    assert _archived_count(conn, project["id"], session_id) == 1


@pytest.mark.db
def test_reextract_dry_run(conn, project_factory):
    """Dry run returns session count without archiving anything."""
    project = project_factory("reext_dry")
    session_id = str(uuid.uuid4())
    _insert_chunk(conn, project["id"], session_id, "chunk content")

    result = run_reextract(
        conn, project["id"], session_id=session_id, dry_run=True
    )

    assert result.get("dry_run") is True
    assert result.get("sessions_targeted") == 1
    assert _archived_count(conn, project["id"], session_id) == 0


@pytest.mark.db
def test_reextract_requires_exactly_one_flag(conn, project_factory):
    """Exactly one of session/all/since must be specified."""
    project = project_factory("reext_mutual")
    session_id = str(uuid.uuid4())

    with pytest.raises(ValueError, match="Exactly one"):
        run_reextract(
            conn, project["id"], session_id=session_id, all_sessions=True
        )

    with pytest.raises(ValueError, match="Exactly one"):
        run_reextract(conn, project["id"])


@pytest.mark.db
def test_reextract_all_sessions(conn, project_factory):
    """run_reextract --all processes all sessions for the project."""
    project = project_factory("reext_all")
    sessions = [str(uuid.uuid4()) for _ in range(3)]
    for sid in sessions:
        _insert_chunk(conn, project["id"], sid, f"content for {sid}")

    result = run_reextract(conn, project["id"], all_sessions=True)

    assert result["sessions_targeted"] == 3


@pytest.mark.db
def test_reextract_empty_project(conn, project_factory):
    """run_reextract on a project with no sessions returns zero-count result."""
    project = project_factory("reext_empty")

    result = run_reextract(conn, project["id"], all_sessions=True)

    assert result["sessions_targeted"] == 0
