"""P_fanout_idempotent: running cognify_fanout twice on the same
session produces no duplicate fan-out rows.

The partial unique index from migration 039
(idx_memory_fanout_unique on (fanout_source_session_id, project_id))
is the enforcement; this test is the verification that the index +
the writer's ON CONFLICT DO NOTHING clause cooperate correctly.
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest


@pytest.mark.db
def test_p_fanout_idempotent(conn, project_factory, request):
    proj_canonical = project_factory("p_fidem_c")
    proj_target = project_factory("p_fidem_t")

    session_id = str(uuid.uuid4())

    def _cleanup_session():
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM devbrain.cognify_spend_log "
                "WHERE project_id IN (%s, %s)",
                (proj_canonical["id"], proj_target["id"]),
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
            VALUES (%s, %s, 'test', '/tmp/p-fidem', %s, 'fidem',
                    'content', now())
            """,
            (session_id, proj_canonical["id"], f"hash-{uuid.uuid4().hex}"),
        )
        cur.execute(
            """
            INSERT INTO devbrain.chunks
                (project_id, source_type, source_id, content)
            VALUES (%s, 'session_summary', %s, 'fidem chunk')
            """,
            (proj_canonical["id"], session_id),
        )
    conn.commit()

    from cognify import fanout as fanout_mod
    from cognify.fanout import ClassificationResult, ProjectClassification

    def stub(sess_id, _conn, **_kw):
        return ClassificationResult(
            session_id=sess_id,
            per_project=[ProjectClassification(
                project_slug=proj_target["slug"],
                session_relevance=0.8,
                section_count=2,
                focused_summary="idempotent fan-out " * 30,
            )],
            usage={
                "input_tokens": 100, "output_tokens": 50,
                "cache_read_tokens": 0, "cache_write_tokens": 0,
            },
        )

    with patch.object(fanout_mod, "classify_session", stub), \
         patch.object(fanout_mod, "_embed_summary", lambda _t: None):
        fanout_mod.run_fanout(conn, project_id=proj_canonical["id"])
        # Second run should re-discover NOTHING (the session now has an
        # active fan-out row, so the anti-join in discover excludes it).
        result_2 = fanout_mod.run_fanout(conn, project_id=proj_canonical["id"])

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory "
            "WHERE fanout_source_session_id = %s "
            "  AND archived_at IS NULL",
            (session_id,),
        )
        count = cur.fetchone()[0]

    assert count == 1, f"expected exactly one fan-out row, got {count}"
    # The second run should have discovered zero sessions (anti-join hit).
    assert result_2.sessions_discovered == 0
