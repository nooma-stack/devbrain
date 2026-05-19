"""Tests for cognify_fanout PR 2 — writer + CLI + pass registration.

Most tests use a live DB + a monkeypatched classifier so we exercise
the real INSERT path / partial unique idempotency seal without
spending on LLM calls.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import psycopg2
import pytest

_FACTORY = Path(__file__).resolve().parents[1]
if str(_FACTORY) not in sys.path:
    sys.path.insert(0, str(_FACTORY))


# ─── apply_shard: pure unit ─────────────────────────────────────────────────


def test_apply_shard_none_passthrough():
    from cognify.fanout import apply_shard
    sessions = ["a", "b", "c", "d", "e"]
    assert apply_shard(sessions, None) == sessions


def test_apply_shard_splits_evenly():
    from cognify.fanout import apply_shard
    sessions = ["a", "b", "c", "d", "e", "f"]
    s0 = apply_shard(sessions, (0, 3))
    s1 = apply_shard(sessions, (1, 3))
    s2 = apply_shard(sessions, (2, 3))
    assert s0 == ["a", "d"]
    assert s1 == ["b", "e"]
    assert s2 == ["c", "f"]
    # Union covers every session, no overlap.
    assert sorted(s0 + s1 + s2) == sorted(sessions)


def test_apply_shard_rejects_bad_args():
    from cognify.fanout import apply_shard
    with pytest.raises(ValueError):
        apply_shard(["a"], (2, 2))  # N == M
    with pytest.raises(ValueError):
        apply_shard(["a"], (-1, 3))


# ─── Live-DB fixtures ───────────────────────────────────────────────────────


def _live_conn():
    from config import DATABASE_URL  # noqa: PLC0415
    return psycopg2.connect(DATABASE_URL)


@pytest.fixture
def live_db():
    if not os.environ.get("DEVBRAIN_DB_PASSWORD") and not os.environ.get("DEVBRAIN_TEST_DATABASE_URL"):
        pytest.skip("DB not configured for tests")
    try:
        conn = _live_conn()
    except Exception as exc:
        pytest.skip(f"live DB unavailable: {exc}")
    yield conn
    conn.close()


@pytest.fixture
def synth_session(live_db):
    """Create a raw_session + one chunk for fan-out tests. Tears down on exit."""
    with live_db.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.projects WHERE slug NOT LIKE 'home-%' "
            "ORDER BY slug LIMIT 1"
        )
        row = cur.fetchone()
        if row is None:
            pytest.skip("no projects in DB")
        canonical_project_id = row[0]

        cur.execute(
            """
            INSERT INTO devbrain.raw_sessions
                (id, project_id, source_app, source_path, source_hash,
                 session_id, started_at, raw_content)
            VALUES (gen_random_uuid(), %s, 'test', '/tmp/synthetic',
                    'test-hash-' || extract(epoch from now())::text,
                    'fanout-test', now(),
                    'Synthetic session content for fan-out tests.')
            RETURNING id
            """,
            (canonical_project_id,),
        )
        session_id = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO devbrain.chunks
                (project_id, source_type, source_id, content)
            VALUES (%s, 'session_summary', %s,
                    'Synthetic session content for fan-out tests')
            """,
            (canonical_project_id, session_id),
        )
        live_db.commit()

    yield {
        "session_id": str(session_id),
        "canonical_project_id": str(canonical_project_id),
    }

    with live_db.cursor() as cur:
        # Cascade-clean: chunks + fan-out memory rows + the raw_session row.
        cur.execute(
            "DELETE FROM devbrain.memory "
            "WHERE fanout_source_session_id = %s",
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
        live_db.commit()


# ─── discover_sessions_needing_fanout ────────────────────────────────────────


def test_discover_finds_new_atomized_session(live_db, synth_session):
    from cognify.fanout import discover_sessions_needing_fanout

    ids = discover_sessions_needing_fanout(live_db)
    assert synth_session["session_id"] in ids


def test_discover_skips_session_with_existing_fanout_row(live_db, synth_session):
    """After a fan-out row exists, discover excludes that session."""
    from cognify.fanout import discover_sessions_needing_fanout

    sess_id = synth_session["session_id"]
    target_pid = synth_session["canonical_project_id"]

    # Plant a fan-out row directly.
    with live_db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO devbrain.memory
                (project_id, kind, title, content, tier, strength,
                 fanout_source_session_id)
            VALUES (%s, 'session_summary', 'plant', 'plant', 'memory', 1.0, %s)
            """,
            (target_pid, sess_id),
        )
        live_db.commit()

    ids = discover_sessions_needing_fanout(live_db)
    assert sess_id not in ids


# ─── run_fanout: end-to-end with mocked classifier ───────────────────────────


def _stub_classification(session_id, conn, **kwargs):
    """Returns a fixed two-target classification — used to test the writer
    without spending on LLM calls."""
    from cognify.fanout import ClassificationResult, ProjectClassification

    # Pick two distinct projects from the live taxonomy.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT slug FROM devbrain.projects WHERE slug NOT LIKE 'home-%' "
            "ORDER BY slug LIMIT 2"
        )
        slugs = [r[0] for r in cur.fetchall()]
    if len(slugs) < 2:
        pytest.skip("need ≥2 non-home projects in DB for two-target test")

    return ClassificationResult(
        session_id=session_id,
        per_project=[
            ProjectClassification(
                project_slug=slugs[0],
                session_relevance=0.85,
                section_count=4,
                focused_summary=(
                    "Synthetic summary A — long enough to look real. "
                    * 8
                ),
            ),
            ProjectClassification(
                project_slug=slugs[1],
                session_relevance=0.45,
                section_count=2,
                focused_summary=(
                    "Synthetic summary B — also long enough. " * 8
                ),
            ),
        ],
        sections=[],
        usage={
            "input_tokens": 1000,
            "output_tokens": 200,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        },
        failure=None,
    )


def test_run_fanout_writes_per_project_rows(live_db, synth_session):
    from cognify import fanout as fanout_mod

    with patch.object(fanout_mod, "classify_session", _stub_classification), \
         patch.object(fanout_mod, "_embed_summary", lambda _t: None):
        result = fanout_mod.run_fanout(
            live_db, max_sessions=10,
        )

    sess_id = synth_session["session_id"]
    with live_db.cursor() as cur:
        cur.execute(
            "SELECT project_id, kind, content FROM devbrain.memory "
            "WHERE fanout_source_session_id = %s "
            "ORDER BY project_id",
            (sess_id,),
        )
        rows = cur.fetchall()
    assert len(rows) == 2, f"expected 2 fan-out rows, got {len(rows)}"
    assert all(r[1] == "session_summary" for r in rows)
    assert result.sessions_processed >= 1
    assert result.rows_emitted >= 2
    # llm_calls reflects every session run_fanout touched (our synthetic
    # plus any other live sessions max_sessions covers); just confirm
    # at least one happened.
    assert result.llm_calls >= 1


def test_run_fanout_idempotent_rerun(live_db, synth_session):
    """Re-running fan-out on the same session emits no new rows."""
    from cognify import fanout as fanout_mod

    with patch.object(fanout_mod, "classify_session", _stub_classification), \
         patch.object(fanout_mod, "_embed_summary", lambda _t: None):
        # First run writes 2 rows.
        result_1 = fanout_mod.run_fanout(live_db, max_sessions=10)
        # Second run discovers nothing (session already has fan-out rows).
        result_2 = fanout_mod.run_fanout(live_db, max_sessions=10)

    sess_id = synth_session["session_id"]
    with live_db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory "
            "WHERE fanout_source_session_id = %s",
            (sess_id,),
        )
        count = cur.fetchone()[0]
    assert count == 2  # unchanged
    assert result_2.sessions_discovered <= result_1.sessions_discovered
    # The session shouldn't appear in run 2's discovery at all.
    from cognify.fanout import discover_sessions_needing_fanout
    assert sess_id not in discover_sessions_needing_fanout(live_db)


def test_run_fanout_dry_run_writes_nothing(live_db, synth_session):
    from cognify import fanout as fanout_mod

    with patch.object(fanout_mod, "classify_session", _stub_classification):
        result = fanout_mod.run_fanout(live_db, max_sessions=10, dry_run=True)

    sess_id = synth_session["session_id"]
    with live_db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory "
            "WHERE fanout_source_session_id = %s",
            (sess_id,),
        )
        count = cur.fetchone()[0]
    assert count == 0
    assert result.dry_run is True
    assert result.sessions_processed == 0
    assert result.rows_emitted == 0


def test_run_fanout_propagates_classifier_failure(live_db, synth_session):
    """A classifier 'empty' failure counts as skipped, not failed."""
    from cognify import fanout as fanout_mod
    from cognify.fanout import ClassificationResult

    def stub_empty(sess_id, conn, **kw):
        return ClassificationResult(
            session_id=sess_id,
            per_project=[],
            failure="empty",
            usage={
                "input_tokens": 500, "output_tokens": 50,
                "cache_read_tokens": 0, "cache_write_tokens": 0,
            },
        )

    with patch.object(fanout_mod, "classify_session", stub_empty), \
         patch.object(fanout_mod, "_embed_summary", lambda _t: None):
        result = fanout_mod.run_fanout(live_db, max_sessions=10)

    assert result.sessions_skipped >= 1
    # And no fan-out rows were written for this session.
    sess_id = synth_session["session_id"]
    with live_db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory "
            "WHERE fanout_source_session_id = %s",
            (sess_id,),
        )
        assert cur.fetchone()[0] == 0


# ─── Orchestrator registration ──────────────────────────────────────────────


def test_fanout_pass_registered_with_orchestrator():
    from cognify.orchestrator import _ensure_registry, _PASS_REGISTRY
    _ensure_registry()
    assert "fanout" in _PASS_REGISTRY
