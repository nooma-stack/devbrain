"""P_end_session_isolation — cross-project payload rejected wholesale.

POSTULATE
---------
If end_session() receives a cascade_decisions payload referencing a
memory from a different project than the session's, the entire payload
is rejected — no partial application. The same rule applies to
new_relationships (every from/to id must belong to the session project).

This is an HIPAA-style cross-project boundary: even one stray id in the
payload fails the whole call. Promotion side-effects on legitimate ids
in the same payload do NOT happen — caller must resubmit a clean
payload.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make `from curator.end_session import ...` resolve from tests/postulates/.
_FACTORY = Path(__file__).resolve().parents[2] / "factory"
if str(_FACTORY) not in sys.path:
    sys.path.insert(0, str(_FACTORY))

from curator.end_session import (  # noqa: E402
    CascadeDecision,
    NewEdge,
    handle_cascade_decisions,
    handle_new_relationships,
)


def test_cross_project_decision_rejected_wholesale(
    conn, project_factory, memory_factory
):
    p1 = project_factory("iso1")
    p2 = project_factory("iso2")
    m_p1 = memory_factory(p1["id"], content="in p1")
    m_p2 = memory_factory(p2["id"], content="in p2")

    decisions = [
        CascadeDecision(
            memory_id=m_p1["id"], action="promote", rationale=""
        ),
        CascadeDecision(
            memory_id=m_p2["id"], action="promote", rationale=""
        ),
    ]

    with pytest.raises(ValueError, match="outside the session's project"):
        handle_cascade_decisions(conn, p1["id"], decisions)

    # Reset the aborted transaction so the post-condition SELECT runs.
    conn.rollback()

    # Verify NO partial application — m_p1 stays at tier='memory'.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tier FROM devbrain.memory WHERE id = %s",
            (m_p1["id"],),
        )
        assert cur.fetchone()[0] == "memory"


def test_cross_project_new_relationship_rejected_wholesale(
    conn, project_factory, memory_factory
):
    p1 = project_factory("rel1")
    p2 = project_factory("rel2")
    a_p1 = memory_factory(p1["id"], content="a in p1")
    b_p1 = memory_factory(p1["id"], content="b in p1")
    c_p2 = memory_factory(p2["id"], content="c in p2")

    edges = [
        NewEdge(
            from_memory_id=a_p1["id"],
            to_memory_id=b_p1["id"],
            edge_type="depends_on",
        ),
        NewEdge(
            from_memory_id=a_p1["id"],
            to_memory_id=c_p2["id"],
            edge_type="depends_on",
        ),
    ]

    with pytest.raises(ValueError, match="outside the session's project"):
        handle_new_relationships(conn, p1["id"], edges)

    conn.rollback()

    # Verify NO partial application — neither edge inserted.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory_dependencies "
            "WHERE from_memory_id = %s",
            (a_p1["id"],),
        )
        assert cur.fetchone()[0] == 0
