"""Tests for Phase 8 PR 1 — foundation pieces only.

Covers:
  * fanout_prompt.render_taxonomy / build_system_prompt / build_user_message
    output shape + spec compliance
  * fanout_prompt.validate_output filters per the locked thresholds
  * migration 039 schema state (home-* projects exist, fanout column +
    partial unique index live)
  * classify_session() error paths that don't require an LLM call
    (no_session, no_content, no_taxonomy)

LLM-bearing happy-path tests for classify_session() are deferred to
PR 2 where they can ride on top of run_fanout's mock harness — at this
foundation stage we cover the wiring + the failure modes that don't
involve an API call.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import psycopg2
import pytest

_FACTORY = Path(__file__).resolve().parents[1]
if str(_FACTORY) not in sys.path:
    sys.path.insert(0, str(_FACTORY))


# ─── fanout_prompt: pure-function unit tests ────────────────────────────────


def test_thresholds_match_locked_spec():
    """Locked values from PR #121 §12.1 (A1, A2)."""
    from cognify.fanout_prompt import (
        SESSION_RELEVANCE_THRESHOLD,
        WITHIN_SECTION_THRESHOLD,
        SUMMARY_MIN_CHARS,
        SUMMARY_MAX_CHARS,
    )
    assert SESSION_RELEVANCE_THRESHOLD == 0.30
    assert WITHIN_SECTION_THRESHOLD == 0.75
    assert SUMMARY_MIN_CHARS == 200
    assert SUMMARY_MAX_CHARS == 800


def test_render_taxonomy_emits_valid_json_with_expected_shape():
    from cognify.fanout_prompt import render_taxonomy
    rendered = render_taxonomy([
        {"slug": "brightbot", "name": "BrightBot", "description": "EMR stuff"},
        {"slug": "pkrelay", "name": "PKRelay", "description": ""},
        {"slug": "devbrain"},  # missing name + description tolerated
    ])
    parsed = json.loads(rendered)
    assert isinstance(parsed, list) and len(parsed) == 3
    assert parsed[0] == {
        "slug": "brightbot",
        "name": "BrightBot",
        "description": "EMR stuff",
    }
    assert parsed[1]["description"] is None  # empty string → None
    assert parsed[2]["name"] == "devbrain"     # name falls back to slug


def test_build_system_prompt_includes_thresholds_and_schema():
    from cognify.fanout_prompt import build_system_prompt, render_taxonomy
    sys_prompt = build_system_prompt(render_taxonomy([
        {"slug": "alpha", "name": "Alpha", "description": "x"},
    ]))
    assert "0.75" in sys_prompt
    assert "0.3" in sys_prompt
    assert "per_project" in sys_prompt
    assert "sections" in sys_prompt
    assert "focused_summary" in sys_prompt


def test_build_user_message_truncates_huge_input():
    from cognify.fanout_prompt import build_user_message
    huge = "x" * 250_000
    msg = build_user_message(huge)
    assert "TRUNCATED" in msg
    assert len(msg) < 210_000


def test_build_user_message_passes_short_input_through():
    from cognify.fanout_prompt import build_user_message
    msg = build_user_message("session content")
    assert "session content" in msg
    assert "TRUNCATED" not in msg


def test_validate_output_drops_below_threshold_entries():
    from cognify.fanout_prompt import validate_output
    parsed = {
        "per_project": [
            {"project_slug": "brightbot", "session_relevance": 0.5,
             "section_count": 3, "focused_summary": "x" * 250},
            {"project_slug": "pkrelay", "session_relevance": 0.15,
             "section_count": 1, "focused_summary": "y" * 250},
            {"project_slug": "devbrain", "session_relevance": 0.30,
             "section_count": 1, "focused_summary": "z" * 250},
        ]
    }
    out = validate_output(parsed, {"brightbot", "pkrelay", "devbrain"})
    slugs = [p["project_slug"] for p in out["per_project"]]
    assert "brightbot" in slugs
    assert "devbrain" in slugs   # at exactly 0.30 — boundary inclusive
    assert "pkrelay" not in slugs


def test_validate_output_drops_unknown_slugs():
    from cognify.fanout_prompt import validate_output
    parsed = {
        "per_project": [
            {"project_slug": "real-project", "session_relevance": 0.5,
             "section_count": 1, "focused_summary": "ok"},
            {"project_slug": "made-up", "session_relevance": 0.9,
             "section_count": 1, "focused_summary": "hallucinated"},
        ]
    }
    out = validate_output(parsed, {"real-project"})
    slugs = [p["project_slug"] for p in out["per_project"]]
    assert slugs == ["real-project"]


def test_validate_output_drops_empty_summaries():
    from cognify.fanout_prompt import validate_output
    parsed = {
        "per_project": [
            {"project_slug": "a", "session_relevance": 0.5,
             "section_count": 1, "focused_summary": "   "},
            {"project_slug": "b", "session_relevance": 0.5,
             "section_count": 1, "focused_summary": "real"},
        ]
    }
    out = validate_output(parsed, {"a", "b"})
    slugs = [p["project_slug"] for p in out["per_project"]]
    assert slugs == ["b"]


def test_validate_output_tolerates_missing_sections_key():
    from cognify.fanout_prompt import validate_output
    out = validate_output({"per_project": []}, set())
    assert out == {"sections": [], "per_project": []}


def test_validate_output_filters_section_scores_to_valid_slugs():
    from cognify.fanout_prompt import validate_output
    parsed = {
        "sections": [
            {"start_turn": 0, "end_turn": 3, "topic": "auth",
             "project_scores": {"alpha": 0.9, "ghost": 0.5}},
        ],
        "per_project": [],
    }
    out = validate_output(parsed, {"alpha"})
    assert out["sections"][0]["project_scores"] == {"alpha": 0.9}


# ─── classify_session: failure-mode tests (no LLM call) ─────────────────────


def _build_conn_with_cursor(fetchone_return, fetchall_returns_seq):
    """Mock conn whose cursor returns a fixed sequence of fetch results."""
    cursor = MagicMock()
    cursor.fetchone.return_value = fetchone_return
    cursor.fetchall.side_effect = fetchall_returns_seq

    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    return conn, cursor


def test_classify_session_returns_no_taxonomy_when_no_projects():
    from cognify.fanout import classify_session

    # Empty taxonomy fetchall.
    conn, _cur = _build_conn_with_cursor(
        fetchone_return=None, fetchall_returns_seq=[[]],
    )
    result = classify_session("00000000-0000-0000-0000-000000000001", conn)
    assert result.failure == "no_taxonomy"


def test_classify_session_returns_no_session_when_unknown_id():
    from cognify.fanout import classify_session

    # Taxonomy: one row. raw_sessions lookup: None.
    conn, _cur = _build_conn_with_cursor(
        fetchone_return=None,
        fetchall_returns_seq=[
            [("alpha", "Alpha", "")],  # taxonomy
        ],
    )
    result = classify_session("00000000-0000-0000-0000-000000000001", conn)
    assert result.failure == "no_session"


def test_classify_session_returns_no_content_when_chunks_empty():
    from cognify.fanout import classify_session

    # taxonomy → 1 row; raw_session exists; chunks fetchall → []
    cursor = MagicMock()
    cursor.fetchone.return_value = ("00000000-0000-0000-0000-000000000001",)
    cursor.fetchall.side_effect = [
        [("alpha", "Alpha", "")],   # taxonomy
        [],                          # chunks
    ]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    result = classify_session("00000000-0000-0000-0000-000000000001", conn)
    assert result.failure == "no_content"


# ─── Migration 039: live-DB schema state ────────────────────────────────────


def _live_conn():
    from config import DATABASE_URL  # noqa: PLC0415
    return psycopg2.connect(DATABASE_URL)


@pytest.fixture
def live_db():
    try:
        conn = _live_conn()
    except Exception as exc:
        pytest.skip(f"live DB unavailable: {exc}")
    yield conn
    conn.close()


def test_migration_039_home_projects_exist_one_per_dev(live_db):
    with live_db.cursor() as cur:
        cur.execute("SELECT dev_id FROM devbrain.devs")
        dev_ids = {r[0] for r in cur.fetchall()}
        cur.execute(
            "SELECT slug FROM devbrain.projects WHERE slug LIKE 'home-%'"
        )
        home_slugs = {r[0] for r in cur.fetchall()}
    # Every dev has a home project (slug = 'home-' + dev_id).
    for d in dev_ids:
        assert f"home-{d}" in home_slugs, f"missing home project for dev {d}"
    # And the orphan catch-all exists.
    assert "home-orphan" in home_slugs


def test_migration_039_fanout_column_present(live_db):
    with live_db.cursor() as cur:
        cur.execute(
            """
            SELECT data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema='devbrain'
              AND table_name='memory'
              AND column_name='fanout_source_session_id'
            """,
        )
        row = cur.fetchone()
    assert row is not None
    data_type, nullable = row
    assert data_type == "uuid"
    assert nullable == "YES"


def test_migration_039_fanout_unique_index_present(live_db):
    with live_db.cursor() as cur:
        cur.execute(
            """
            SELECT indexdef FROM pg_indexes
            WHERE schemaname='devbrain'
              AND indexname='idx_memory_fanout_unique'
            """,
        )
        row = cur.fetchone()
    assert row is not None
    indexdef = row[0]
    # Partial unique on (fanout_source_session_id, project_id) with the
    # kind/tier/archived predicate.
    assert "UNIQUE" in indexdef
    assert "fanout_source_session_id" in indexdef
    assert "project_id" in indexdef
    assert "session_summary" in indexdef
    assert "archived_at IS NULL" in indexdef
