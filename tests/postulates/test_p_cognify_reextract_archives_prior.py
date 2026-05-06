"""P_cognify_reextract_archives_prior: cognify-reextract archives prior rows
(sets archived_at); doesn't delete; new rows carry reextracted_from metadata.
"""
from __future__ import annotations

import uuid

import pytest

from cognify.extract import _upsert_memory, _archive_prior_extracts


def _count_archived(conn, project_id, session_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory "
            "WHERE project_id = %s "
            "  AND tier = 'lesson' AND archived_at IS NOT NULL "
            "  AND (applies_when->>'source_session') = %s",
            (project_id, session_id),
        )
        return cur.fetchone()[0]


def _count_total(conn, project_id, session_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory "
            "WHERE project_id = %s "
            "  AND tier = 'lesson' "
            "  AND (applies_when->>'source_session') = %s",
            (project_id, session_id),
        )
        return cur.fetchone()[0]


@pytest.mark.db
def test_p_cognify_reextract_archives_prior(conn, project_factory):
    """Re-extract archives existing rows (sets archived_at) and doesn't delete.

    Also verifies that a re-inserted row can carry reextracted_from metadata.
    """
    project = project_factory("p_reext_arch")
    session_id = str(uuid.uuid4())

    # Insert original lesson.
    original_id, _ = _upsert_memory(
        conn,
        project_id=project["id"],
        kind="lesson",
        title="Original Lesson",
        content="Original content",
        session_id=session_id,
    )

    # Archive prior extracts (simulates the first step of re-extraction).
    archived_count = _archive_prior_extracts(conn, session_id, project["id"])

    # Row must be archived, not deleted.
    assert archived_count == 1
    assert _count_archived(conn, project["id"], session_id) == 1
    assert _count_total(conn, project["id"], session_id) == 1

    # Insert a new row with reextracted_from metadata.
    new_id, new_was_new = _upsert_memory(
        conn,
        project_id=project["id"],
        kind="lesson",
        title="Re-extracted Lesson",
        content="Better content after re-extraction",
        session_id=session_id,
        reextract=True,
        reextract_meta=str(original_id),
    )

    assert new_was_new is True
    assert new_id != original_id

    # Verify new row has reextracted_from in its applies_when JSONB.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT applies_when FROM devbrain.memory WHERE id = %s", (new_id,)
        )
        meta = cur.fetchone()[0] or {}
    assert meta.get("reextracted_from") == str(original_id)
