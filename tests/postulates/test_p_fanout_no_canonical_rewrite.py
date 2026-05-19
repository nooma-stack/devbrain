"""P_fanout_no_canonical_rewrite: run_fanout never modifies
raw_sessions.project_id on existing sessions.

This invariant protects the provenance_id chains in devbrain.memory —
thousands of atoms reference their source raw_session by id, and a
canonical-project rewrite would break atom-to-session lineage queries
even if the underlying session content didn't change.

Spec: docs/plans/2026-05-11-phase-8-cross-project-fan-out-design.md
§12.4 ("Canonical assignment policy"): "Existing rows: canonical stays
put. Period. Even when classifier disagrees..."
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest


@pytest.mark.db
def test_p_fanout_no_canonical_rewrite(conn, project_factory, request):
    """Plant a session in project A, run fan-out with a stub classifier
    that claims it 'belongs to' project B, confirm raw_sessions.project_id
    is unchanged."""
    proj_a = project_factory("p_fnocr_a")
    proj_b = project_factory("p_fnocr_b")

    # Create a raw_session canonical-owned by project A.
    session_id = str(uuid.uuid4())

    def _cleanup_session():
        # Delete the raw_session + dependent chunks BEFORE project_factory's
        # cleanup runs — otherwise raw_sessions_project_id_fkey blocks the
        # project DELETE. Also clean cognify_spend_log rows that run_fanout
        # writes (FK project_id blocks project delete too).
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM devbrain.cognify_spend_log "
                "WHERE project_id IN (%s, %s)",
                (proj_a["id"], proj_b["id"]),
            )
            cur.execute(
                "DELETE FROM devbrain.memory WHERE fanout_source_session_id = %s",
                (session_id,),
            )
            cur.execute(
                "DELETE FROM devbrain.chunks WHERE source_id = %s",
                (session_id,),
            )
            cur.execute(
                "DELETE FROM devbrain.raw_sessions WHERE id = %s",
                (session_id,),
            )
        conn.commit()
    request.addfinalizer(_cleanup_session)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO devbrain.raw_sessions
                (id, project_id, source_app, source_path, source_hash,
                 session_id, raw_content, started_at)
            VALUES (%s, %s, 'test', '/tmp/p-fnocr', %s, 'fnocr', 'content', now())
            """,
            (session_id, proj_a["id"], f"hash-{uuid.uuid4().hex}"),
        )
        # And give it a chunk so discovery picks it up.
        cur.execute(
            """
            INSERT INTO devbrain.chunks
                (project_id, source_type, source_id, content)
            VALUES (%s, 'session_summary', %s, 'chunk for P_fnocr')
            """,
            (proj_a["id"], session_id),
        )
    conn.commit()

    # Stub classifier emits a fan-out target into project B.
    from cognify import fanout as fanout_mod
    from cognify.fanout import (
        ClassificationResult, ProjectClassification,
    )

    def stub(sess_id, _conn, **_kw):
        return ClassificationResult(
            session_id=sess_id,
            per_project=[ProjectClassification(
                project_slug=proj_b["slug"],
                session_relevance=0.9,
                section_count=3,
                focused_summary="reassign content " * 30,
            )],
            usage={
                "input_tokens": 100, "output_tokens": 50,
                "cache_read_tokens": 0, "cache_write_tokens": 0,
            },
        )

    with patch.object(fanout_mod, "classify_session", stub), \
         patch.object(fanout_mod, "_embed_summary", lambda _t: None):
        # Scope discovery to the test session by passing project_id=proj_a.
        fanout_mod.run_fanout(conn, project_id=proj_a["id"])

    # Verify: raw_sessions.project_id still proj_a.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT project_id FROM devbrain.raw_sessions WHERE id = %s",
            (session_id,),
        )
        canonical = cur.fetchone()[0]
    assert str(canonical) == str(proj_a["id"]), (
        f"canonical should stay {proj_a['id']}; saw {canonical}"
    )
    # AND the fan-out row landed in project B (writer fired).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT project_id FROM devbrain.memory "
            "WHERE fanout_source_session_id = %s",
            (session_id,),
        )
        targets = [str(r[0]) for r in cur.fetchall()]
    assert str(proj_b["id"]) in targets
