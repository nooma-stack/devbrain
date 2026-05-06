"""Integration tests for cognify_edges pass.

Covers:
  - _detect_cites finds text-based cross-references
  - _insert_edge is idempotent (ON CONFLICT DO NOTHING)
  - EdgesPass.run requires project_id
  - EdgesPass.run dry_run doesn't insert edges
  - _llm_judge_contradiction gracefully returns False when no API key
"""
from __future__ import annotations

import uuid

import pytest

from cognify.edges import (
    EDGE_TYPE_CITES,
    EDGE_TYPE_CONTRADICTS,
    EdgesPass,
    _detect_cites,
    _insert_edge,
    _llm_judge_contradiction,
)


def _insert_memory_with_content(conn, project_id, title, content, kind="decision"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory "
            "(project_id, kind, title, content) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (project_id, kind, title, content),
        )
        mid = cur.fetchone()[0]
    conn.commit()
    return mid


def _count_edges_of_type(conn, from_id, to_id, edge_type):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory_dependencies "
            "WHERE from_memory_id = %s AND to_memory_id = %s AND edge_type = %s",
            (from_id, to_id, edge_type),
        )
        return cur.fetchone()[0]


@pytest.mark.db
def test_detect_cites_finds_title_reference(conn, project_factory):
    """_detect_cites creates a cites edge when memory A mentions memory B's title."""
    project = project_factory("edges_cites")
    title_b = "AuthFlowDecision"
    m_a = _insert_memory_with_content(
        conn, project["id"], "FeatureSpec",
        f"This relates to the {title_b} we made last sprint."
    )
    m_b = _insert_memory_with_content(
        conn, project["id"], title_b,
        "We decided to use OAuth2 for authentication."
    )

    new_edges = _detect_cites(conn, project["id"])

    assert new_edges >= 1
    assert _count_edges_of_type(conn, m_a, m_b, EDGE_TYPE_CITES) == 1


@pytest.mark.db
def test_detect_cites_no_self_reference(conn, project_factory):
    """_detect_cites doesn't create self-referential cites edges."""
    project = project_factory("edges_self")
    m = _insert_memory_with_content(
        conn, project["id"], "SelfTitle",
        "SelfTitle is itself, which should not create a self-edge."
    )

    new_edges = _detect_cites(conn, project["id"])

    assert _count_edges_of_type(conn, m, m, EDGE_TYPE_CITES) == 0


@pytest.mark.db
def test_insert_edge_idempotent(conn, project_factory, memory_factory):
    """_insert_edge with ON CONFLICT DO NOTHING inserts only once."""
    project = project_factory("edges_idem")
    m_a = memory_factory(project["id"])
    m_b = memory_factory(project["id"])

    inserted_1 = _insert_edge(
        conn,
        from_id=m_a["id"],
        to_id=m_b["id"],
        edge_type=EDGE_TYPE_CITES,
        confidence=0.7,
        created_by="test",
    )
    inserted_2 = _insert_edge(
        conn,
        from_id=m_a["id"],
        to_id=m_b["id"],
        edge_type=EDGE_TYPE_CITES,
        confidence=0.7,
        created_by="test",
    )

    assert inserted_1 == 1
    assert inserted_2 == 0
    assert _count_edges_of_type(conn, m_a["id"], m_b["id"], EDGE_TYPE_CITES) == 1


@pytest.mark.db
def test_edges_pass_requires_project_id(conn):
    """EdgesPass.run raises ValueError when project_id is None."""
    pass_ = EdgesPass()
    with pytest.raises(ValueError, match="project_id"):
        pass_.run(conn, None)


@pytest.mark.db
def test_edges_pass_dry_run_no_edges(conn, project_factory):
    """EdgesPass dry_run doesn't insert any edges."""
    project = project_factory("edges_dry")
    # Insert two memories where one cites the other's title.
    title_b = "DryRunTarget"
    _insert_memory_with_content(
        conn, project["id"], "Source",
        f"References {title_b} in its content."
    )
    _insert_memory_with_content(
        conn, project["id"], title_b, "Target memory."
    )

    pass_ = EdgesPass()
    result = pass_.run(conn, project["id"], dry_run=True)

    # No actual edges should exist
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory_dependencies "
            "WHERE from_memory_id IN ("
            "  SELECT id FROM devbrain.memory WHERE project_id = %s"
            ")",
            (project["id"],),
        )
        edge_count = cur.fetchone()[0]

    assert edge_count == 0


def test_llm_judge_contradiction_returns_false_without_api_key(monkeypatch):
    """_llm_judge_contradiction returns (False, empty_usage) when API key is not set."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    contradicts, usage = _llm_judge_contradiction("Memory A says X.", "Memory B says not X.")
    assert contradicts is False
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0
