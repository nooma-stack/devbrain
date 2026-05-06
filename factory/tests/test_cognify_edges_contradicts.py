"""Tests for cognify_edges contradicts detection via LLM + graph_walk.

All LLM calls are mocked — no real API calls are made during pytest.

Covers:
  - _llm_judge_contradiction returns False when API key is missing.
  - _llm_judge_contradiction returns True when mocked response is YES.
  - _llm_judge_contradiction returns False when mocked response is NO.
  - _detect_contradicts inserts bidirectional edges when LLM says YES.
  - _detect_contradicts honours the 15-call cost ceiling.
  - _detect_contradicts is idempotent (ON CONFLICT DO NOTHING).
  - _detect_contradicts dry_run: counts pairs but inserts nothing.
  - _detect_contradicts uses tier-filtered seeds (lesson/rule first; falls back
    to all memories if none exist).
  - Project isolation: contradicts detection is scoped to the given project.
  - _detect_contradicts skips pairs with missing content gracefully.
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from cognify.edges import (
    CONTRADICTION_SEED_TIERS,
    EDGE_TYPE_CONTRADICTS,
    MAX_LLM_CALLS_PER_PASS,
    _detect_contradicts,
    _llm_judge_contradiction,
)

# After PR #103 (spend tracking), _llm_judge_contradiction returns
# (bool, usage_dict). All bool-only mocks below get this empty usage shape
# so the production unpack at the call site doesn't TypeError.
_NO_USAGE = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
}


# ── helpers ───────────────────────────────────────────────────────────────────


def _insert_mem(conn, project_id, title, content, kind="decision", tier="memory"):
    """Insert a memory row directly; returns its UUID."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory "
            "(project_id, kind, tier, title, content) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (project_id, kind, tier, title, content),
        )
        mid = cur.fetchone()[0]
    conn.commit()
    return mid


def _insert_edge_direct(conn, from_id, to_id, edge_type, confidence=1.0):
    """Insert a memory_dependencies edge directly for test setup."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, confidence, created_by) "
            "VALUES (%s, %s, %s, %s, 'test') ON CONFLICT DO NOTHING",
            (from_id, to_id, edge_type, confidence),
        )
    conn.commit()


def _edge_count(conn, from_id, to_id, edge_type):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory_dependencies "
            "WHERE from_memory_id = %s AND to_memory_id = %s AND edge_type = %s",
            (from_id, to_id, edge_type),
        )
        return cur.fetchone()[0]


def _total_contradicts_edges(conn, project_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory_dependencies md "
            "JOIN devbrain.memory m ON m.id = md.from_memory_id "
            "WHERE m.project_id = %s AND md.edge_type = %s",
            (project_id, EDGE_TYPE_CONTRADICTS),
        )
        return cur.fetchone()[0]


# ── unit tests (no DB) ────────────────────────────────────────────────────────


def test_llm_judge_returns_false_without_api_key(monkeypatch):
    """No ANTHROPIC_API_KEY → graceful False, no exception."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    flag, _usage = _llm_judge_contradiction("A says always do X.", "B says never do X.")
    assert flag is False


def test_llm_judge_returns_true_on_yes_response(monkeypatch):
    """Mocked YES response → True.

    anthropic is imported lazily inside _llm_judge_contradiction, so we patch
    via sys.modules rather than module-level attribute.
    """
    import sys

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text="YES")]

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_msg

    mock_anthropic_mod = MagicMock()
    mock_anthropic_mod.Anthropic.return_value = mock_client

    monkeypatch.setitem(sys.modules, "anthropic", mock_anthropic_mod)

    flag, _usage = _llm_judge_contradiction("Always cache results.", "Never cache results.")

    assert flag is True
    mock_client.messages.create.assert_called_once()


def test_llm_judge_returns_false_on_no_response(monkeypatch):
    """Mocked NO response → False."""
    import sys

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

    mock_msg = MagicMock()
    mock_msg.content = [MagicMock(text="NO")]

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_msg

    mock_anthropic_mod = MagicMock()
    mock_anthropic_mod.Anthropic.return_value = mock_client

    monkeypatch.setitem(sys.modules, "anthropic", mock_anthropic_mod)

    flag, _usage = _llm_judge_contradiction("Use async patterns.", "Use async patterns.")

    assert flag is False


def test_llm_judge_graceful_on_api_exception(monkeypatch):
    """If the API raises, _llm_judge_contradiction returns False (no crash)."""
    import sys

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = RuntimeError("network error")

    mock_anthropic_mod = MagicMock()
    mock_anthropic_mod.Anthropic.return_value = mock_client

    monkeypatch.setitem(sys.modules, "anthropic", mock_anthropic_mod)

    flag, _usage = _llm_judge_contradiction("A.", "B.")

    assert flag is False


def test_contradiction_seed_tiers_constant():
    """CONTRADICTION_SEED_TIERS contains lesson and rule."""
    assert "lesson" in CONTRADICTION_SEED_TIERS
    assert "rule" in CONTRADICTION_SEED_TIERS


# ── integration tests (require DB) ───────────────────────────────────────────


@pytest.mark.db
def test_detect_contradicts_inserts_bidirectional_edges(conn, project_factory):
    """When LLM says YES, both A→B and B→A contradicts edges are inserted."""
    project = project_factory("contra_bidir")

    # Two lesson memories connected by a derived_from edge so walk finds them.
    m_a = _insert_mem(
        conn, project["id"], "LessonA",
        "Always commit database changes immediately.",
        kind="decision", tier="lesson",
    )
    m_b = _insert_mem(
        conn, project["id"], "LessonB",
        "Never commit database changes without a review.",
        kind="decision", tier="lesson",
    )
    _insert_edge_direct(conn, m_a, m_b, "derived_from")

    with patch("cognify.edges._llm_judge_contradiction", return_value=(True, _NO_USAGE)):
        new_edges, llm_calls = _detect_contradicts(conn, project["id"])

    assert new_edges == 2  # bidirectional
    assert llm_calls == 1
    assert _edge_count(conn, m_a, m_b, EDGE_TYPE_CONTRADICTS) == 1
    assert _edge_count(conn, m_b, m_a, EDGE_TYPE_CONTRADICTS) == 1


@pytest.mark.db
def test_detect_contradicts_no_edge_when_llm_says_no(conn, project_factory):
    """When LLM says NO, no contradicts edges are inserted."""
    project = project_factory("contra_no")

    m_a = _insert_mem(
        conn, project["id"], "RuleA",
        "Use typed parameters in SQL.",
        kind="decision", tier="rule",
    )
    m_b = _insert_mem(
        conn, project["id"], "RuleB",
        "Always validate input at the API boundary.",
        kind="decision", tier="rule",
    )
    _insert_edge_direct(conn, m_a, m_b, "derived_from")

    with patch("cognify.edges._llm_judge_contradiction", return_value=(False, _NO_USAGE)):
        new_edges, llm_calls = _detect_contradicts(conn, project["id"])

    assert new_edges == 0
    assert llm_calls >= 1
    assert _total_contradicts_edges(conn, project["id"]) == 0


@pytest.mark.db
def test_detect_contradicts_idempotent(conn, project_factory):
    """Running _detect_contradicts twice inserts edges only on the first pass."""
    project = project_factory("contra_idem")

    m_a = _insert_mem(
        conn, project["id"], "LessonX",
        "Always use connection pooling.",
        kind="decision", tier="lesson",
    )
    m_b = _insert_mem(
        conn, project["id"], "LessonY",
        "Never use connection pooling in scripts.",
        kind="decision", tier="lesson",
    )
    _insert_edge_direct(conn, m_a, m_b, "derived_from")

    with patch("cognify.edges._llm_judge_contradiction", return_value=(True, _NO_USAGE)):
        first_new, _ = _detect_contradicts(conn, project["id"])
        second_new, _ = _detect_contradicts(conn, project["id"])

    assert first_new == 2  # A→B and B→A
    assert second_new == 0  # ON CONFLICT DO NOTHING


@pytest.mark.db
def test_detect_contradicts_dry_run_no_insert(conn, project_factory):
    """dry_run=True counts contradicts but inserts nothing."""
    project = project_factory("contra_dry")

    m_a = _insert_mem(
        conn, project["id"], "LessonDry",
        "Use pessimistic locking.",
        kind="decision", tier="lesson",
    )
    m_b = _insert_mem(
        conn, project["id"], "RuleDry",
        "Never use pessimistic locking.",
        kind="decision", tier="rule",
    )
    _insert_edge_direct(conn, m_a, m_b, "derived_from")

    with patch("cognify.edges._llm_judge_contradiction", return_value=(True, _NO_USAGE)):
        new_edges, llm_calls = _detect_contradicts(conn, project["id"], dry_run=True)

    assert new_edges >= 1
    assert llm_calls >= 1
    # Nothing actually inserted.
    assert _total_contradicts_edges(conn, project["id"]) == 0


@pytest.mark.db
def test_detect_contradicts_respects_llm_call_ceiling(conn, project_factory):
    """At most MAX_LLM_CALLS_PER_PASS (15) LLM calls are made per pass."""
    project = project_factory("contra_ceil")

    # Create 20 lesson memories all chained by derived_from, yielding many pairs.
    memory_ids = []
    for i in range(20):
        mid = _insert_mem(
            conn, project["id"], f"Lesson{i:02d}",
            f"Statement number {i}.",
            kind="decision", tier="lesson",
        )
        memory_ids.append(mid)

    # Connect them into a chain so the walker finds neighbors.
    for i in range(len(memory_ids) - 1):
        _insert_edge_direct(conn, memory_ids[i], memory_ids[i + 1], "derived_from")

    call_count = {"n": 0}

    def counting_judge(a, b):
        call_count["n"] += 1
        return False, _NO_USAGE  # no edges, just counting calls

    with patch("cognify.edges._llm_judge_contradiction", side_effect=counting_judge):
        _detect_contradicts(conn, project["id"])

    assert call_count["n"] <= MAX_LLM_CALLS_PER_PASS


@pytest.mark.db
def test_detect_contradicts_fallback_when_no_lesson_rule_tiers(conn, project_factory):
    """When no lesson/rule rows exist, falls back to all memories (e.g. new project)."""
    project = project_factory("contra_fallback")

    # Only 'decision' tier memories connected by derived_from.
    m_a = _insert_mem(
        conn, project["id"], "DecisionA",
        "We chose PostgreSQL.",
        kind="decision", tier="memory",
    )
    m_b = _insert_mem(
        conn, project["id"], "DecisionB",
        "We chose SQLite.",
        kind="decision", tier="memory",
    )
    _insert_edge_direct(conn, m_a, m_b, "derived_from")

    with patch("cognify.edges._llm_judge_contradiction", return_value=(True, _NO_USAGE)):
        new_edges, llm_calls = _detect_contradicts(conn, project["id"])

    # Fallback activated: still finds and judges the pair.
    assert llm_calls >= 1
    assert new_edges == 2


@pytest.mark.db
def test_detect_contradicts_project_isolation(conn, project_factory):
    """Contradicts detection in project A does not see memories from project B."""
    proj_a = project_factory("contra_iso_a")
    proj_b = project_factory("contra_iso_b")

    # Two lesson memories in project B, connected.
    b1 = _insert_mem(
        conn, proj_b["id"], "B_LessonX",
        "Always do X.",
        kind="decision", tier="lesson",
    )
    b2 = _insert_mem(
        conn, proj_b["id"], "B_LessonY",
        "Never do X.",
        kind="decision", tier="lesson",
    )
    _insert_edge_direct(conn, b1, b2, "derived_from")

    # One lesson memory in project A (no neighbors → no pairs → no LLM calls).
    _insert_mem(
        conn, proj_a["id"], "A_Lesson",
        "Do something.",
        kind="decision", tier="lesson",
    )

    call_count = {"n": 0}

    def spy(a, b):
        call_count["n"] += 1
        return True, _NO_USAGE

    with patch("cognify.edges._llm_judge_contradiction", side_effect=spy):
        _detect_contradicts(conn, proj_a["id"])

    # Project A has only one memory → no candidate pairs → 0 LLM calls.
    assert call_count["n"] == 0
    assert _total_contradicts_edges(conn, proj_a["id"]) == 0


@pytest.mark.db
def test_detect_contradicts_skips_empty_content_pairs(conn, project_factory):
    """Pairs where either memory has empty content are skipped without LLM call."""
    project = project_factory("contra_empty")

    m_a = _insert_mem(
        conn, project["id"], "LessonFull",
        "A substantive claim about the system.",
        kind="decision", tier="lesson",
    )
    # m_b has empty content — should be skipped.
    m_b = _insert_mem(
        conn, project["id"], "LessonEmpty",
        "",
        kind="decision", tier="lesson",
    )
    _insert_edge_direct(conn, m_a, m_b, "derived_from")

    call_count = {"n": 0}

    def spy(a, b):
        call_count["n"] += 1
        return False, _NO_USAGE

    with patch("cognify.edges._llm_judge_contradiction", side_effect=spy):
        _detect_contradicts(conn, project["id"])

    # Empty content → pair skipped → 0 LLM calls.
    assert call_count["n"] == 0
