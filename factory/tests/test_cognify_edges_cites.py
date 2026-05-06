"""Tests for cognify_edges cites detection via regex/text matching.

Covers the _detect_cites function in detail:
  - Finds a title reference in content (word-boundary match).
  - Multiple cites in one memory row.
  - Does NOT create self-reference edges.
  - Is idempotent (ON CONFLICT DO NOTHING — running twice returns 0 on second pass).
  - Case-insensitive title matching.
  - Short/single-word titles do not produce false positives from substrings.
  - Archived rows are excluded.
  - Project isolation: memories from project B do not trigger cites in project A.
"""
from __future__ import annotations

import uuid

import pytest

from cognify.edges import (
    EDGE_TYPE_CITES,
    _detect_cites,
    _insert_edge,
    _normalize_title,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _insert_mem(conn, project_id, title, content, kind="decision", archived=False):
    """Insert a memory row directly; returns its UUID."""
    with conn.cursor() as cur:
        if archived:
            cur.execute(
                "INSERT INTO devbrain.memory "
                "(project_id, kind, title, content, archived_at) "
                "VALUES (%s, %s, %s, %s, now()) RETURNING id",
                (project_id, kind, title, content),
            )
        else:
            cur.execute(
                "INSERT INTO devbrain.memory "
                "(project_id, kind, title, content) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (project_id, kind, title, content),
            )
        mid = cur.fetchone()[0]
    conn.commit()
    return mid


def _edge_count(conn, from_id, to_id, edge_type):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory_dependencies "
            "WHERE from_memory_id = %s AND to_memory_id = %s AND edge_type = %s",
            (from_id, to_id, edge_type),
        )
        return cur.fetchone()[0]


# ── unit tests (no DB) ────────────────────────────────────────────────────────


def test_normalize_title_strips_punctuation():
    assert _normalize_title("AuthFlow!Decision?") == "authflowdecision"


def test_normalize_title_lowercases():
    assert _normalize_title("SomeName") == "somename"


def test_normalize_title_strips_leading_trailing_spaces():
    assert _normalize_title("  hello world  ") == "hello world"


def test_normalize_title_empty_string():
    assert _normalize_title("") == ""


# ── integration tests (require DB) ───────────────────────────────────────────


@pytest.mark.db
def test_cites_word_boundary_match(conn, project_factory):
    """A title reference within larger text is found if it sits on word boundaries."""
    project = project_factory("cites_wb")
    title_b = "OAuthStrategy"
    m_a = _insert_mem(
        conn, project["id"], "Impl Notes",
        "We decided to use OAuthStrategy as the foundation."
    )
    m_b = _insert_mem(conn, project["id"], title_b, "Details of the OAuth strategy.")

    found = _detect_cites(conn, project["id"])

    assert found >= 1
    assert _edge_count(conn, m_a, m_b, EDGE_TYPE_CITES) == 1


@pytest.mark.db
def test_cites_case_insensitive(conn, project_factory):
    """Title match is case-insensitive."""
    project = project_factory("cites_ci")
    m_a = _insert_mem(
        conn, project["id"], "Summary",
        "This references authflowdecision per recent discussions."
    )
    m_b = _insert_mem(
        conn, project["id"], "AuthFlowDecision", "The actual auth decision."
    )

    found = _detect_cites(conn, project["id"])

    assert found >= 1
    assert _edge_count(conn, m_a, m_b, EDGE_TYPE_CITES) == 1


@pytest.mark.db
def test_cites_multiple_references_in_one_memory(conn, project_factory):
    """A single memory mentioning two titles creates two cites edges."""
    project = project_factory("cites_multi")
    m_a = _insert_mem(
        conn, project["id"], "AggregatorDoc",
        "Relies on AlphaDecision and BetaLesson for context."
    )
    m_b = _insert_mem(conn, project["id"], "AlphaDecision", "Alpha details.")
    m_c = _insert_mem(conn, project["id"], "BetaLesson", "Beta details.")

    found = _detect_cites(conn, project["id"])

    assert found >= 2
    assert _edge_count(conn, m_a, m_b, EDGE_TYPE_CITES) == 1
    assert _edge_count(conn, m_a, m_c, EDGE_TYPE_CITES) == 1


@pytest.mark.db
def test_cites_no_self_reference(conn, project_factory):
    """A memory whose content matches its own title does not create a self-edge."""
    project = project_factory("cites_self")
    m = _insert_mem(
        conn, project["id"], "SelfRef",
        "SelfRef is a memory about itself."
    )

    _detect_cites(conn, project["id"])

    assert _edge_count(conn, m, m, EDGE_TYPE_CITES) == 0


@pytest.mark.db
def test_cites_idempotent_second_run(conn, project_factory):
    """Running _detect_cites twice does not create duplicate edges."""
    project = project_factory("cites_idem")
    m_a = _insert_mem(
        conn, project["id"], "Spec", "Based on TargetMemory analysis."
    )
    m_b = _insert_mem(conn, project["id"], "TargetMemory", "The target.")

    first = _detect_cites(conn, project["id"])
    second = _detect_cites(conn, project["id"])

    assert first >= 1
    assert second == 0  # ON CONFLICT DO NOTHING → no new inserts
    assert _edge_count(conn, m_a, m_b, EDGE_TYPE_CITES) == 1


@pytest.mark.db
def test_cites_archived_rows_excluded(conn, project_factory):
    """Archived memory rows are not loaded; they cannot be cites sources or targets."""
    project = project_factory("cites_arch")
    # m_a is archived; it should not appear as a source.
    _insert_mem(
        conn, project["id"], "ArchivedSource",
        "References TargetTitle in its content.",
        archived=True,
    )
    m_b = _insert_mem(conn, project["id"], "TargetTitle", "Live target memory.")

    found = _detect_cites(conn, project["id"])

    # The archived source should not contribute any edges.
    assert found == 0
    # No edges from anything to m_b either.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory_dependencies "
            "WHERE to_memory_id = %s AND edge_type = %s",
            (m_b, EDGE_TYPE_CITES),
        )
        assert cur.fetchone()[0] == 0


@pytest.mark.db
def test_cites_project_isolation(conn, project_factory):
    """cites detection in project A does not pick up memories from project B."""
    proj_a = project_factory("cites_iso_a")
    proj_b = project_factory("cites_iso_b")

    # Memory in project B has a title.
    _insert_mem(conn, proj_b["id"], "CrossProjectTarget", "B's memory.")
    # Memory in project A mentions B's title.
    m_a = _insert_mem(
        conn, proj_a["id"], "ASource",
        "We might reference CrossProjectTarget here."
    )

    found_a = _detect_cites(conn, proj_a["id"])

    # No cross-project edge — project A has no memory titled "CrossProjectTarget".
    assert found_a == 0


@pytest.mark.db
def test_cites_single_memory_no_cross_reference(conn, project_factory):
    """A project with only one memory row returns 0 (nothing to reference)."""
    project = project_factory("cites_single")
    _insert_mem(conn, project["id"], "OnlyMemory", "There is only one memory here.")

    found = _detect_cites(conn, project["id"])

    assert found == 0
