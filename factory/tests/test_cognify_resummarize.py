"""Tests for cognify_resummarize.

Most tests mock _call_sonnet so the live-DB happy-path doesn't burn
on actual LLM calls. Migration 041 schema state + discovery logic are
exercised against the real DB.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import psycopg2
import pytest

_FACTORY = Path(__file__).resolve().parents[1]
if str(_FACTORY) not in sys.path:
    sys.path.insert(0, str(_FACTORY))


def _live_conn():
    from config import DATABASE_URL  # noqa: PLC0415
    return psycopg2.connect(DATABASE_URL)


@pytest.fixture
def live_db():
    if not os.environ.get("DEVBRAIN_DB_PASSWORD") and not os.environ.get("DEVBRAIN_TEST_DATABASE_URL"):
        pytest.skip("DB not configured for tests")
    conn = _live_conn()
    yield conn
    conn.close()


@pytest.fixture
def synth_session(live_db):
    """A raw_session + one session_summary chunk, marked summary_source='ollama'.
    Yields the session_id; teardown removes the row + dependents.

    Creates a dedicated test project so discovery can scope to just this
    fixture's session — avoids picking up unrelated rows when max_sessions
    is set low and the global session ordering would push the synth row
    out of range.
    """
    test_slug = f"resum-test-{uuid.uuid4().hex[:8]}"
    with live_db.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.projects (slug, name) VALUES (%s, %s) RETURNING id",
            (test_slug, "resummarize test"),
        )
        project_id = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO devbrain.raw_sessions
                (id, project_id, source_app, source_path, source_hash,
                 session_id, started_at, raw_content, summary, summary_source,
                 created_at)
            VALUES (gen_random_uuid(), %s, 'test',
                    '/tmp/resum-synth-' || extract(epoch from now())::text,
                    'h-' || extract(epoch from now())::text,
                    'resum-test-' || extract(epoch from now())::text,
                    now() - interval '1 hour',  -- settled
                    'Synthetic session content for resummarize testing. '
                      || repeat('Lorem ipsum dolor sit amet. ', 50),
                    'short ollama summary',
                    'ollama',
                    now() - interval '1 hour'   -- created earlier, past settled threshold
                    )
            RETURNING id
            """,
            (project_id,),
        )
        session_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO devbrain.chunks
                (project_id, source_type, source_id, content)
            VALUES (%s, 'session_summary', %s, 'short ollama summary')
            """,
            (project_id, session_id),
        )
        live_db.commit()

    yield {
        "session_id": str(session_id),
        "project_id": str(project_id),
    }

    with live_db.cursor() as cur:
        cur.execute(
            "DELETE FROM devbrain.cognify_spend_log WHERE project_id = %s",
            (project_id,),
        )
        cur.execute(
            "DELETE FROM devbrain.chunks WHERE source_id = %s",
            (session_id,),
        )
        cur.execute(
            "DELETE FROM devbrain.raw_sessions WHERE id = %s",
            (session_id,),
        )
        cur.execute(
            "DELETE FROM devbrain.projects WHERE id = %s",
            (project_id,),
        )
        live_db.commit()


# ─── Migration 041 schema state ─────────────────────────────────────────────


def test_migration_041_summary_source_column_present(live_db):
    with live_db.cursor() as cur:
        cur.execute(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema='devbrain'
              AND table_name='raw_sessions'
              AND column_name='summary_source'
            """,
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "character varying"


def test_migration_041_index_present(live_db):
    with live_db.cursor() as cur:
        cur.execute(
            """
            SELECT indexdef FROM pg_indexes
            WHERE schemaname='devbrain'
              AND indexname='idx_raw_sessions_summary_source'
            """,
        )
        row = cur.fetchone()
    assert row is not None
    assert "summary_source" in row[0]
    assert "summary IS NOT NULL" in row[0]


# ─── discover_sessions_needing_resummarize ──────────────────────────────────


def test_discover_finds_orphan_ollama_session(live_db, synth_session):
    from cognify.resummarize import discover_sessions_needing_resummarize

    discovered = discover_sessions_needing_resummarize(live_db)
    ids = [r[0] for r in discovered]
    assert synth_session["session_id"] in ids


def test_discover_skips_session_with_end_session_log(live_db, synth_session):
    """If end_session_log has a row matching the session's session_id (text),
    the discover query excludes it."""
    from cognify.resummarize import discover_sessions_needing_resummarize

    # Plant an end_session_log row keyed on our synth session_id.
    with live_db.cursor() as cur:
        cur.execute(
            "SELECT session_id FROM devbrain.raw_sessions WHERE id = %s",
            (synth_session["session_id"],),
        )
        text_sid = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO devbrain.end_session_log
                (session_id, payload_hash, project_id, result)
            VALUES (%s, 'planted-hash', %s, '{}'::jsonb)
            """,
            (text_sid, synth_session["project_id"]),
        )
        live_db.commit()

    try:
        discovered = discover_sessions_needing_resummarize(live_db)
        ids = [r[0] for r in discovered]
        assert synth_session["session_id"] not in ids
    finally:
        with live_db.cursor() as cur:
            cur.execute(
                "DELETE FROM devbrain.end_session_log "
                "WHERE session_id = %s AND payload_hash = 'planted-hash'",
                (text_sid,),
            )
            live_db.commit()


def test_discover_skips_already_upgraded_session(live_db, synth_session):
    """summary_source='sonnet' rows are excluded from re-discovery."""
    from cognify.resummarize import discover_sessions_needing_resummarize

    with live_db.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.raw_sessions SET summary_source='sonnet' WHERE id=%s",
            (synth_session["session_id"],),
        )
        live_db.commit()

    discovered = discover_sessions_needing_resummarize(live_db)
    ids = [r[0] for r in discovered]
    assert synth_session["session_id"] not in ids


# ─── run_resummarize end-to-end (mocked LLM) ─────────────────────────────────


def _stub_sonnet(_raw, _model):
    """Returns (summary, usage, failure) — mirrors _call_sonnet signature."""
    return (
        "A higher-quality sonnet summary describing what happened. " * 8,
        {
            "input_tokens": 1500, "output_tokens": 250,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
        },
        None,
    )


def test_run_resummarize_upgrades_summary(live_db, synth_session):
    from cognify import resummarize as resum_mod

    with patch.object(resum_mod, "_call_sonnet", _stub_sonnet):
        result = resum_mod.run_resummarize(live_db, project_id=synth_session["project_id"], max_sessions=10)

    assert result.sessions_processed >= 1
    assert result.sessions_failed == 0
    assert result.llm_calls >= 1

    # Verify the row was actually updated.
    with live_db.cursor() as cur:
        cur.execute(
            "SELECT summary_source, summary FROM devbrain.raw_sessions "
            "WHERE id = %s",
            (synth_session["session_id"],),
        )
        source, summary = cur.fetchone()
    assert source == "sonnet"
    assert "sonnet summary" in summary


def test_run_resummarize_dry_run_no_writes(live_db, synth_session):
    from cognify import resummarize as resum_mod

    with patch.object(resum_mod, "_call_sonnet", _stub_sonnet):
        result = resum_mod.run_resummarize(live_db, project_id=synth_session["project_id"], dry_run=True, max_sessions=10)

    assert result.dry_run is True
    assert result.sessions_processed == 0

    # Summary unchanged.
    with live_db.cursor() as cur:
        cur.execute(
            "SELECT summary_source FROM devbrain.raw_sessions WHERE id = %s",
            (synth_session["session_id"],),
        )
        assert cur.fetchone()[0] == "ollama"


def test_run_resummarize_idempotent(live_db, synth_session):
    """Second run after a successful first upgrade discovers nothing."""
    from cognify import resummarize as resum_mod

    with patch.object(resum_mod, "_call_sonnet", _stub_sonnet):
        first = resum_mod.run_resummarize(live_db, project_id=synth_session["project_id"], max_sessions=10)
        # Session is now summary_source='sonnet'; second run skips it.
        second = resum_mod.run_resummarize(live_db, project_id=synth_session["project_id"], max_sessions=10)

    # Our synth session is no longer in second.discovered.
    with live_db.cursor() as cur:
        cur.execute(
            "SELECT summary_source FROM devbrain.raw_sessions WHERE id = %s",
            (synth_session["session_id"],),
        )
        assert cur.fetchone()[0] == "sonnet"
    # first must have processed at least our session; second can have 0
    # to many other live sessions but our session is now excluded.
    from cognify.resummarize import discover_sessions_needing_resummarize
    remaining = discover_sessions_needing_resummarize(live_db)
    assert synth_session["session_id"] not in [r[0] for r in remaining]


def test_run_resummarize_failure_propagates_label(live_db, synth_session):
    from cognify import resummarize as resum_mod

    def stub_fail(_raw, _model):
        return None, {
            "input_tokens": 100, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
        }, "empty"

    with patch.object(resum_mod, "_call_sonnet", stub_fail):
        result = resum_mod.run_resummarize(live_db, project_id=synth_session["project_id"], max_sessions=10)

    assert result.sessions_failed >= 1
    assert result.failure_counts.get("empty", 0) >= 1
    # Our synth session was not upgraded.
    with live_db.cursor() as cur:
        cur.execute(
            "SELECT summary_source FROM devbrain.raw_sessions WHERE id = %s",
            (synth_session["session_id"],),
        )
        assert cur.fetchone()[0] == "ollama"


# ─── Pass registration ───────────────────────────────────────────────────────


def test_resummarize_pass_registered():
    from cognify.orchestrator import _ensure_registry, _PASS_REGISTRY

    _ensure_registry()
    assert "resummarize" in _PASS_REGISTRY


# ─── Launchd installer includes resummarize plist ────────────────────────────


def test_launchd_installer_includes_resummarize():
    from cognify.setup_launchd import _CRED_PLISTS, ALL_PLISTS

    assert "com.devbrain.cognify-resummarize.plist" in _CRED_PLISTS
    assert "com.devbrain.cognify-resummarize.plist" in ALL_PLISTS
