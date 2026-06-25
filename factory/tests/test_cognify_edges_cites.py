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

import random
import re as _re

from cognify.edges import (
    EDGE_TYPE_CITES,
    _detect_cites,
    _insert_edge,
    _load_memories,
    _normalize_title,
)


def _reference_cites_edges(mems):
    """The ORIGINAL O(N×T) cites logic, returning the set of (from, to)
    string-id pairs. Used to prove the optimized _detect_cites is exactly
    equivalent. `mems` is a list of dicts with str 'id', 'title', 'content'."""
    title_index = {}
    for m in mems:
        if m["title"]:
            norm = _normalize_title(m["title"])
            if norm:
                title_index[norm] = m["id"]
    edges = set()
    for m in mems:
        content = m["content"] or ""
        for norm_title, target_id in title_index.items():
            if target_id == m["id"]:
                continue
            if _re.search(
                r"\b" + _re.escape(norm_title) + r"\b", content, _re.IGNORECASE
            ):
                edges.add((m["id"], target_id))
    return edges


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


@pytest.mark.db
def test_cites_substring_not_word_boundary_no_edge(conn, project_factory):
    """A title that appears only as a substring of a LARGER word must NOT
    cite. This guards the substring pre-filter added for performance: the
    cheap `in` check passes (the title is a substring), but the
    word-boundary regex must still reject it — so no edge is created."""
    project = project_factory("cites_substr")
    # "Auth" is a substring of "Authentication" but not a whole word there.
    m_a = _insert_mem(
        conn, project["id"], "Caller",
        "We rely on Authentication throughout the service."
    )
    m_b = _insert_mem(conn, project["id"], "Auth", "The Auth subsystem.")

    found = _detect_cites(conn, project["id"])

    assert found == 0
    assert _edge_count(conn, m_a, m_b, EDGE_TYPE_CITES) == 0


@pytest.mark.db
def test_cites_shared_memories_snapshot_equivalent(conn, project_factory):
    """Passing a pre-loaded `memories` snapshot (the edges-pass load-once
    path) produces the identical cite count as letting _detect_cites load
    its own. dry_run avoids inserting so both paths see the same data."""
    project = project_factory("cites_snapshot")
    _insert_mem(
        conn, project["id"], "Impl Notes",
        "We chose OAuthStrategy and also FeatureFlags."
    )
    _insert_mem(conn, project["id"], "OAuthStrategy", "Details.")
    _insert_mem(conn, project["id"], "FeatureFlags", "More details.")

    internal = _detect_cites(conn, project["id"], dry_run=True)
    snapshot = _load_memories(conn, project["id"])
    shared = _detect_cites(
        conn, project["id"], dry_run=True, memories=snapshot
    )

    assert internal == shared >= 2


@pytest.mark.db
def test_cites_equivalence_vs_reference_random(conn, project_factory):
    """The first-word-index _detect_cites must produce the EXACT same edge
    set as the original O(N×T) regex scan, over randomized data that stresses
    the tricky cases: word-bounded hits, case differences, multi-word titles,
    titles glued inside a larger word (substring but no boundary → no edge),
    and untitled source rows. Compares the real DB edges to a reference."""
    rng = random.Random(20260625)
    project = project_factory("cites_equiv_rand")

    # Distinct titles (no normalized-title collisions → no last-wins ambiguity).
    titles = [
        "OAuthStrategy", "Auth", "Feature Flags", "DB-Migration", "retry logic",
        "CacheLayer", "Cache", "Login Flow", "login", "RBAC",
    ]
    mems = []
    # 10 titled rows (one per title) + 15 untitled rows; every row gets content
    # that embeds random titles in random forms.
    plan = [(t, True) for t in titles] + [(None, False) for _ in range(15)]
    rng.shuffle(plan)
    for title, _titled in plan:
        parts = ["lorem ipsum dolor"]
        for _ in range(rng.randint(0, 4)):
            t = rng.choice(titles)
            mode = rng.randint(0, 3)
            if mode == 0:
                parts.append(t)                       # word-bounded → edge
            elif mode == 1:
                parts.append(t.upper())               # case-insensitive → edge
            elif mode == 2:
                parts.append("xx" + t.replace(" ", "") + "yy")  # glued → no edge
            else:
                parts.append(t.lower())               # word-bounded lower → edge
        content = " ".join(parts) + " the end."
        mems.append({"title": title, "content": content})

    for m in mems:
        m["id"] = str(_insert_mem(conn, project["id"], m["title"], m["content"]))

    _detect_cites(conn, project["id"])

    ids = [m["id"] for m in mems]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT from_memory_id, to_memory_id FROM devbrain.memory_dependencies "
            "WHERE edge_type = %s AND from_memory_id = ANY(%s::uuid[])",
            (EDGE_TYPE_CITES, ids),
        )
        actual = {(str(a), str(b)) for a, b in cur.fetchall()}

    reference = _reference_cites_edges(mems)

    assert actual == reference
    assert reference  # sanity: the random data actually produced some edges
