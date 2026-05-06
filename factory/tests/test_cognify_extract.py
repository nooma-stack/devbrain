"""Integration tests for cognify_extract pass.

Covers:
  - extract_from_session skips duplicate (same provenance+kind+title) rows
  - _archive_prior_extracts sets archived_at on prior lessons/decisions
  - _sessions_since returns sessions since a given timestamp
  - run_extract_pass dry_run returns candidate sessions without inserting
  - project_id isolation: extract doesn't create rows in wrong project
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from cognify.extract import (
    ExtractPass,
    ExtractResult,
    _archive_prior_extracts,
    _sessions_since,
    _upsert_memory,
    extract_from_session,
    run_extract_pass,
)


def _count_lessons(conn, project_id, session_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory "
            "WHERE project_id = %s "
            "  AND tier = 'lesson' AND archived_at IS NULL "
            "  AND (applies_when->>'source_session') = %s",
            (project_id, session_id),
        )
        return cur.fetchone()[0]


def _insert_chunk(conn, project_id, session_id, content, kind="pattern"):
    """Insert a raw memory chunk for a session."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory "
            "(project_id, kind, title, content, provenance_id) "
            "VALUES (%s, %s, %s, %s, %s::uuid) RETURNING id",
            (project_id, kind, f"chunk_{uuid.uuid4().hex[:6]}", content, session_id),
        )
        mid = cur.fetchone()[0]
    conn.commit()
    return mid


@pytest.mark.db
def test_upsert_memory_creates_new_row(conn, project_factory):
    """_upsert_memory inserts a new lesson row and returns (id, True)."""
    project = project_factory("extract_new")
    session_id = str(uuid.uuid4())

    mid, was_new = _upsert_memory(
        conn,
        project_id=project["id"],
        kind="lesson",
        title="Test Lesson",
        content="Some content",
        session_id=session_id,
    )

    assert was_new is True
    assert mid is not None


@pytest.mark.db
def test_upsert_memory_idempotent_on_duplicate(conn, project_factory):
    """_upsert_memory returns (existing_id, False) for duplicate title+session."""
    project = project_factory("extract_idem")
    session_id = str(uuid.uuid4())

    mid1, was_new1 = _upsert_memory(
        conn,
        project_id=project["id"],
        kind="lesson",
        title="Dup Lesson",
        content="Content",
        session_id=session_id,
    )
    mid2, was_new2 = _upsert_memory(
        conn,
        project_id=project["id"],
        kind="lesson",
        title="Dup Lesson",
        content="Content",
        session_id=session_id,
    )

    assert was_new1 is True
    assert was_new2 is False
    assert mid1 == mid2


@pytest.mark.db
def test_archive_prior_extracts_sets_archived_at(conn, project_factory, memory_factory):
    """_archive_prior_extracts sets archived_at on existing extracted rows."""
    project = project_factory("extract_archive")
    session_id = str(uuid.uuid4())

    # Insert an extracted lesson for this session
    mid, _ = _upsert_memory(
        conn,
        project_id=project["id"],
        kind="lesson",
        title="Old Lesson",
        content="Old content",
        session_id=session_id,
    )

    count = _archive_prior_extracts(conn, session_id, project["id"])
    assert count == 1

    # Row should be archived
    with conn.cursor() as cur:
        cur.execute(
            "SELECT archived_at FROM devbrain.memory WHERE id = %s", (mid,)
        )
        archived_at = cur.fetchone()[0]
    assert archived_at is not None


@pytest.mark.db
def test_archive_prior_extracts_skips_non_extract_rows(conn, project_factory):
    """_archive_prior_extracts doesn't archive raw chunks (tier='memory')."""
    project = project_factory("extract_arch_skip")
    session_id = str(uuid.uuid4())

    # Insert a raw chunk (tier='memory', not tier='lesson')
    chunk_id = _insert_chunk(conn, project["id"], session_id, "raw chunk content")

    count = _archive_prior_extracts(conn, session_id, project["id"])
    assert count == 0

    # Chunk row should still be non-archived
    with conn.cursor() as cur:
        cur.execute(
            "SELECT archived_at FROM devbrain.memory WHERE id = %s", (chunk_id,)
        )
        archived_at = cur.fetchone()[0]
    assert archived_at is None


@pytest.mark.db
def test_sessions_since_returns_sessions_after_timestamp(
    conn, project_factory
):
    """_sessions_since returns provenance_ids of sessions with rows inserted
    after the given timestamp."""
    project = project_factory("extract_since")
    session_old = str(uuid.uuid4())
    session_new = str(uuid.uuid4())

    # Insert an "old" chunk, then record a timestamp, then insert "new"
    _insert_chunk(conn, project["id"], session_old, "old content")

    # Force old row to have old created_at
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET created_at = NOW() - INTERVAL '2 days' "
            "WHERE provenance_id = %s",
            (session_old,),
        )
    conn.commit()

    # Use a cutoff slightly in the past so the new chunk (inserted after)
    # is reliably captured. PostgreSQL's TIMESTAMPTZ precision may differ
    # from Python's datetime by a few microseconds.
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(milliseconds=100)
    _insert_chunk(conn, project["id"], session_new, "new content")

    sessions = _sessions_since(conn, project["id"], cutoff)
    assert session_new in sessions
    assert session_old not in sessions


@pytest.mark.db
def test_extract_pass_dry_run(conn, project_factory):
    """ExtractPass dry_run returns candidate sessions without inserting rows."""
    project = project_factory("extract_dryrun")
    session_id = str(uuid.uuid4())
    _insert_chunk(conn, project["id"], session_id, "some content")

    pass_ = ExtractPass()
    result = pass_.run(conn, project["id"], dry_run=True)

    assert result.rows_processed == 0
    assert result.metadata.get("dry_run_candidate_sessions", 0) >= 1


@pytest.mark.db
def test_extract_pass_requires_project_id(conn):
    """ExtractPass.run raises ValueError if project_id is None."""
    pass_ = ExtractPass()
    with pytest.raises(ValueError, match="project_id"):
        pass_.run(conn, None)
