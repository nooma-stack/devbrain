"""P_fanout_relevance_threshold_honored: fan-out rows are only emitted
when session-level relevance is >= 0.30 (the locked threshold from
PR #121 §12.1 A1).

The classifier might emit borderline entries (model occasionally
fudges on the boundary); the defensive validator + writer must drop
anything below the floor before it lands in devbrain.memory.
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest


@pytest.mark.db
def test_p_fanout_relevance_threshold_honored(conn, project_factory, request):
    proj_canonical = project_factory("p_frth_c")
    proj_above = project_factory("p_frth_above")
    proj_below = project_factory("p_frth_below")

    session_id = str(uuid.uuid4())

    def _cleanup_session():
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM devbrain.cognify_spend_log "
                "WHERE project_id IN (%s, %s, %s)",
                (proj_canonical["id"], proj_above["id"], proj_below["id"]),
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
            VALUES (%s, %s, 'test', '/tmp/p-frth', %s, 'frth',
                    'content', now())
            """,
            (session_id, proj_canonical["id"], f"hash-{uuid.uuid4().hex}"),
        )
        cur.execute(
            """
            INSERT INTO devbrain.chunks
                (project_id, source_type, source_id, content)
            VALUES (%s, 'session_summary', %s, 'frth chunk')
            """,
            (proj_canonical["id"], session_id),
        )
    conn.commit()

    # The classifier raw output: one above-threshold, one below. The
    # fanout writer should drop the below one. We patch *_parse_json_with_fallbacks*
    # to bypass the model and feed the validator the raw mixture directly.
    from cognify import fanout as fanout_mod

    raw_parsed = {
        "sections": [],
        "per_project": [
            {
                "project_slug": proj_above["slug"],
                "session_relevance": 0.5,
                "section_count": 2,
                "focused_summary": "above threshold " * 30,
            },
            {
                "project_slug": proj_below["slug"],
                "session_relevance": 0.20,   # below 0.30 floor
                "section_count": 1,
                "focused_summary": "below threshold " * 30,
            },
        ],
    }

    # Stub at the classify_session level — the validator runs inside
    # classify_session, so feed it through there.
    from cognify.fanout import ClassificationResult, ProjectClassification
    from cognify.fanout_prompt import validate_output

    valid_slugs = {proj_above["slug"], proj_below["slug"]}
    validated = validate_output(raw_parsed, valid_slugs)
    # Sanity: validator dropped the below row.
    assert {p["project_slug"] for p in validated["per_project"]} == {proj_above["slug"]}

    def stub(sess_id, _conn, **_kw):
        return ClassificationResult(
            session_id=sess_id,
            per_project=[
                ProjectClassification(
                    project_slug=e["project_slug"],
                    session_relevance=e["session_relevance"],
                    section_count=e["section_count"],
                    focused_summary=e["focused_summary"],
                )
                for e in validated["per_project"]
            ],
            usage={
                "input_tokens": 100, "output_tokens": 50,
                "cache_read_tokens": 0, "cache_write_tokens": 0,
            },
        )

    with patch.object(fanout_mod, "classify_session", stub), \
         patch.object(fanout_mod, "_embed_summary", lambda _t: None):
        fanout_mod.run_fanout(conn, project_id=proj_canonical["id"])

    with conn.cursor() as cur:
        cur.execute(
            "SELECT project_id FROM devbrain.memory "
            "WHERE fanout_source_session_id = %s",
            (session_id,),
        )
        target_ids = [str(r[0]) for r in cur.fetchall()]

    assert str(proj_above["id"]) in target_ids
    assert str(proj_below["id"]) not in target_ids, (
        "below-threshold project must not receive a fan-out row"
    )
