"""P_cognify_idempotent_extract: running cognify_extract twice on the same
session produces no duplicate rows.
"""
from __future__ import annotations

import uuid

import pytest

from cognify.extract import extract_from_session, _upsert_memory


def _insert_chunk(conn, project_id, session_id):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory "
            "(project_id, kind, title, content, provenance_id) "
            "VALUES (%s, 'pattern', %s, 'chunk content', %s::uuid) RETURNING id",
            (project_id, f"chunk_{uuid.uuid4().hex[:6]}", session_id),
        )
        mid = cur.fetchone()[0]
    conn.commit()
    return mid


def _active_extract_count(conn, project_id, session_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory "
            "WHERE project_id = %s "
            "  AND tier = 'lesson' AND archived_at IS NULL "
            "  AND (applies_when->>'source_session') = %s",
            (project_id, session_id),
        )
        return cur.fetchone()[0]


@pytest.mark.db
def test_p_cognify_idempotent_extract(conn, project_factory):
    """Running upsert_memory twice with the same args creates exactly one row."""
    project = project_factory("p_idem_ext")
    session_id = str(uuid.uuid4())
    _insert_chunk(conn, project["id"], session_id)

    # First upsert
    mid1, new1 = _upsert_memory(
        conn,
        project_id=project["id"],
        kind="lesson",
        title="Idempotent Lesson",
        content="Content A",
        session_id=session_id,
    )
    # Second upsert (same session + kind + title)
    mid2, new2 = _upsert_memory(
        conn,
        project_id=project["id"],
        kind="lesson",
        title="Idempotent Lesson",
        content="Content A",
        session_id=session_id,
    )

    assert new1 is True
    assert new2 is False
    assert mid1 == mid2
    assert _active_extract_count(conn, project["id"], session_id) == 1
