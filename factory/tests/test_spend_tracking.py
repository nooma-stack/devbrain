"""Tests for LLM spend tracking (migration 029 + observability package).

Covers:
  - pricing.compute_cost_usd: correct USD cost from token counts
  - pricing.get_pricing: registry lookup
  - spend.record_spend: inserts a row into cognify_spend_log
  - spend.record_spend: failure is non-fatal (no exception raised)
  - Daily view: cognify_spend_daily aggregates by (project_id, day, model)
  - extract.py integration: spend row written when mock LLM returns usage
  - edges.py integration: spend row written when mock LLM returns usage
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from observability.pricing import (
    SONNET_4_6,
    ModelPricing,
    compute_cost_usd,
    get_pricing,
)
from observability.spend import record_spend


# ── Pricing unit tests (no DB) ────────────────────────────────────────────────


def test_compute_cost_usd_input_only():
    """1M input tokens at $3/Mtok = $3.00."""
    cost = compute_cost_usd(SONNET_4_6, input_tokens=1_000_000, output_tokens=0)
    assert abs(cost - 3.00) < 1e-9


def test_compute_cost_usd_output_only():
    """1M output tokens at $15/Mtok = $15.00."""
    cost = compute_cost_usd(SONNET_4_6, input_tokens=0, output_tokens=1_000_000)
    assert abs(cost - 15.00) < 1e-9


def test_compute_cost_usd_cache_read():
    """1M cache_read tokens at $0.30/Mtok = $0.30."""
    cost = compute_cost_usd(
        SONNET_4_6, input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000
    )
    assert abs(cost - 0.30) < 1e-9


def test_compute_cost_usd_cache_write():
    """1M cache_write tokens at $3.75/Mtok = $3.75."""
    cost = compute_cost_usd(
        SONNET_4_6, input_tokens=0, output_tokens=0, cache_write_tokens=1_000_000
    )
    assert abs(cost - 3.75) < 1e-9


def test_compute_cost_usd_mixed():
    """Combined token counts produce the correct sum."""
    cost = compute_cost_usd(
        SONNET_4_6,
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=200,
        cache_write_tokens=300,
    )
    expected = (
        (100 / 1_000_000) * 3.00
        + (50 / 1_000_000) * 15.00
        + (200 / 1_000_000) * 0.30
        + (300 / 1_000_000) * 3.75
    )
    assert abs(cost - expected) < 1e-12


def test_compute_cost_usd_zero():
    """Zero tokens → zero cost."""
    cost = compute_cost_usd(SONNET_4_6, input_tokens=0, output_tokens=0)
    assert cost == 0.0


def test_get_pricing_known_model():
    """get_pricing returns SONNET_4_6 for 'claude-sonnet-4-6'."""
    p = get_pricing("claude-sonnet-4-6")
    assert p is SONNET_4_6
    assert isinstance(p, ModelPricing)


def test_get_pricing_unknown_model():
    """get_pricing returns None for unregistered models."""
    p = get_pricing("claude-nonexistent-999")
    assert p is None


# ── DB integration tests ──────────────────────────────────────────────────────


def _count_spend_rows(conn, project_id) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.cognify_spend_log "
            "WHERE project_id = %s",
            (project_id,),
        )
        return cur.fetchone()[0]


def _fetch_spend_rows(conn, project_id) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pass_name, model, input_tokens, output_tokens, "
            "       cache_read_tokens, cache_write_tokens, cost_usd "
            "FROM devbrain.cognify_spend_log "
            "WHERE project_id = %s "
            "ORDER BY id",
            (project_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


@pytest.mark.db
def test_record_spend_inserts_row(conn, project_factory):
    """record_spend inserts a cognify_spend_log row with correct values."""
    project = project_factory("spend_basic")

    cost = compute_cost_usd(
        SONNET_4_6,
        input_tokens=1000,
        output_tokens=200,
        cache_read_tokens=500,
        cache_write_tokens=0,
    )
    row_id = record_spend(
        conn,
        project_id=project["id"],
        pass_name="extract",
        model="claude-sonnet-4-6",
        input_tokens=1000,
        output_tokens=200,
        cache_read_tokens=500,
        cache_write_tokens=0,
        cost_usd=cost,
    )

    assert row_id is not None
    rows = _fetch_spend_rows(conn, project["id"])
    assert len(rows) == 1
    r = rows[0]
    assert r["pass_name"] == "extract"
    assert r["model"] == "claude-sonnet-4-6"
    assert r["input_tokens"] == 1000
    assert r["output_tokens"] == 200
    assert r["cache_read_tokens"] == 500
    assert r["cache_write_tokens"] == 0
    assert abs(float(r["cost_usd"]) - cost) < 1e-6


@pytest.mark.db
def test_record_spend_multiple_rows(conn, project_factory):
    """Multiple record_spend calls produce independent rows."""
    project = project_factory("spend_multi")

    for i in range(3):
        record_spend(
            conn,
            project_id=project["id"],
            pass_name="edges",
            model="claude-sonnet-4-6",
            input_tokens=100 * (i + 1),
            output_tokens=10,
            cost_usd=0.001,
        )

    assert _count_spend_rows(conn, project["id"]) == 3


@pytest.mark.db
def test_record_spend_nonfatal_on_bad_conn(project_factory):
    """record_spend returns None and doesn't raise on a broken connection."""
    bad_conn = MagicMock()
    bad_conn.cursor.side_effect = Exception("connection closed")

    result = record_spend(
        bad_conn,
        project_id=uuid.uuid4(),
        pass_name="extract",
        model="claude-sonnet-4-6",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.001,
    )
    assert result is None


@pytest.mark.db
def test_cognify_spend_daily_view(conn, project_factory):
    """cognify_spend_daily aggregates rows by (project_id, day, model)."""
    project = project_factory("spend_daily")

    # Insert 2 rows with the same project/model (they should aggregate).
    record_spend(
        conn,
        project_id=project["id"],
        pass_name="extract",
        model="claude-sonnet-4-6",
        input_tokens=1000,
        output_tokens=100,
        cost_usd=0.0035,
    )
    record_spend(
        conn,
        project_id=project["id"],
        pass_name="extract",
        model="claude-sonnet-4-6",
        input_tokens=2000,
        output_tokens=200,
        cost_usd=0.0090,
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT input_tokens, output_tokens, cost_usd, call_count "
            "FROM devbrain.cognify_spend_daily "
            "WHERE project_id = %s AND model = %s",
            (project["id"], "claude-sonnet-4-6"),
        )
        rows = cur.fetchall()

    assert len(rows) == 1
    input_tok, output_tok, cost, call_count = rows[0]
    assert input_tok == 3000
    assert output_tok == 300
    assert call_count == 2
    assert abs(float(cost) - 0.0125) < 1e-6


@pytest.mark.db
def test_extract_pass_records_spend(conn, project_factory):
    """extract_from_session writes a spend row when the mock LLM returns usage."""
    from cognify.extract import extract_from_session

    project = project_factory("spend_extract")
    session_id = str(uuid.uuid4())

    # Insert a raw chunk
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory "
            "(project_id, kind, title, content, provenance_id) "
            "VALUES (%s, 'pattern', 'chunk', 'some content', %s::uuid) RETURNING id",
            (project["id"], session_id),
        )
    conn.commit()

    # Mock _llm_extract to return usage data
    mock_response = {
        "lessons": [{"title": "Test Lesson", "content": "Content about something"}],
        "decisions": [],
        "_usage": {
            "input_tokens": 500,
            "output_tokens": 100,
            "cache_read_tokens": 200,
            "cache_write_tokens": 0,
        },
    }
    with patch("cognify.extract._llm_extract", return_value=mock_response):
        result = extract_from_session(conn, session_id, project["id"])

    assert result.llm_calls == 1
    assert result.lessons_created == 1

    # Spend row should have been written
    rows = _fetch_spend_rows(conn, project["id"])
    assert len(rows) == 1
    r = rows[0]
    assert r["pass_name"] == "extract"
    assert r["input_tokens"] == 500
    assert r["output_tokens"] == 100
    assert r["cache_read_tokens"] == 200


@pytest.mark.db
def test_extract_pass_no_spend_when_no_tokens(conn, project_factory):
    """extract_from_session does not write a spend row when token counts are 0
    (e.g. no API key in CI — LLM returns empty usage)."""
    from cognify.extract import extract_from_session

    project = project_factory("spend_extract_no_tok")
    session_id = str(uuid.uuid4())

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory "
            "(project_id, kind, title, content, provenance_id) "
            "VALUES (%s, 'pattern', 'chunk2', 'content', %s::uuid) RETURNING id",
            (project["id"], session_id),
        )
    conn.commit()

    mock_response = {
        "lessons": [],
        "decisions": [],
        "_usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        },
    }
    with patch("cognify.extract._llm_extract", return_value=mock_response):
        extract_from_session(conn, session_id, project["id"])

    assert _count_spend_rows(conn, project["id"]) == 0


@pytest.mark.db
def test_edges_pass_records_spend(conn, project_factory, memory_factory):
    """_detect_contradicts writes a spend row when the mock LLM returns usage."""
    project = project_factory("spend_edges")
    # Insert two memory rows that will form a candidate pair via the walker.
    memory_factory(project["id"], kind="decision", title="Edge node A", content="A says X")
    memory_factory(project["id"], kind="decision", title="Edge node B", content="B says not X")

    # The walker won't find edges without real graph topology; we test at
    # the _detect_contradicts level by mocking _llm_judge_contradiction
    # directly to simulate a real LLM hit.
    usage_stub = {
        "input_tokens": 300,
        "output_tokens": 5,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }

    with patch("cognify.edges._llm_judge_contradiction", return_value=(False, usage_stub)):
        # We need candidate pairs to trigger the loop. Patch walk to return
        # the two memory nodes as neighbors of each other.
        from types import SimpleNamespace

        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM devbrain.memory WHERE project_id = %s AND archived_at IS NULL",
                (project["id"],),
            )
            mem_ids = [r[0] for r in cur.fetchall()]

        if len(mem_ids) < 2:
            pytest.skip("need at least 2 memory rows for edge spend test")

        # Manually invoke _detect_contradicts with mocked walk that returns pairs.
        from unittest.mock import patch as _patch
        from cognify import edges as edges_mod

        # Build a fake walk result returning the second node as neighbor of first.
        fake_neighbor = SimpleNamespace(id=mem_ids[1])
        fake_walk_result = SimpleNamespace(memories=[fake_neighbor])

        with _patch.object(edges_mod, "walk", return_value=fake_walk_result):
            edges_mod._detect_contradicts(
                conn, project["id"], record_conn=conn
            )

    # Spend row should exist for the judged pair
    rows = _fetch_spend_rows(conn, project["id"])
    assert len(rows) >= 1
    assert rows[0]["pass_name"] == "edges"
    assert rows[0]["input_tokens"] == 300
