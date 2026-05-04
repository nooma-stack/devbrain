"""Integration tests for the end_session enrichment handlers.

end_session_idempotent_handler is the public entry point: applies
cascade_decisions, new_relationships, lesson_candidates volunteered by
the calling agent, then drains the cascade queue. Idempotent on
(session_id, payload_hash); same hash → return prior result without
re-applying side-effects.

These tests use a real Postgres (devbrain-db). They commit mid-test
because the handlers commit after each phase.
"""
from __future__ import annotations

import pytest

from curator.end_session import (
    CascadeDecision,
    LessonCandidate,
    NewEdge,
    end_session_idempotent_handler,
    handle_cascade_decisions,
    handle_lesson_candidates,
    handle_new_relationships,
)


@pytest.mark.db
def test_handle_cascade_decisions_promote(
    conn, project_factory, memory_factory
):
    project = project_factory("ces1")
    m = memory_factory(project["id"], content="x")

    handle_cascade_decisions(
        conn,
        project["id"],
        [
            CascadeDecision(
                memory_id=m["id"], action="promote", rationale="ranks high"
            )
        ],
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT tier FROM devbrain.memory WHERE id = %s", (m["id"],)
        )
        assert cur.fetchone()[0] == "lesson"


@pytest.mark.db
def test_handle_cascade_decisions_contradict_halves_strength(
    conn, project_factory, memory_factory
):
    project = project_factory("ces2")
    m = memory_factory(project["id"], content="x")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET strength = 0.8 WHERE id = %s",
            (m["id"],),
        )
    conn.commit()

    handle_cascade_decisions(
        conn,
        project["id"],
        [CascadeDecision(memory_id=m["id"], action="contradict")],
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT strength FROM devbrain.memory WHERE id = %s", (m["id"],)
        )
        assert float(cur.fetchone()[0]) == pytest.approx(0.4, abs=0.01)


@pytest.mark.db
def test_handle_cascade_decisions_refine_enqueues_self(
    conn, project_factory, memory_factory
):
    """`refine` enqueues a self-cascade signal so Step 6 can pick it up."""
    project = project_factory("ces3")
    m = memory_factory(project["id"], content="x")

    handle_cascade_decisions(
        conn,
        project["id"],
        [CascadeDecision(memory_id=m["id"], action="refine")],
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT memory_id, cascade_source_id, edge_type "
            "FROM devbrain.curator_re_eval_queue WHERE memory_id = %s",
            (m["id"],),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == m["id"]
    assert row[1] == m["id"]  # self-cascade
    assert row[2] == "applies_when"


@pytest.mark.db
def test_handle_new_relationships_inserts_edge(
    conn, project_factory, memory_factory
):
    project = project_factory("ces4")
    a = memory_factory(project["id"], content="a")
    b = memory_factory(project["id"], content="b")

    handle_new_relationships(
        conn,
        project["id"],
        [
            NewEdge(
                from_memory_id=a["id"],
                to_memory_id=b["id"],
                edge_type="depends_on",
            )
        ],
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT created_by, edge_type FROM devbrain.memory_dependencies "
            "WHERE from_memory_id = %s AND to_memory_id = %s",
            (a["id"], b["id"]),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "end_session"
    assert row[1] == "depends_on"


@pytest.mark.db
def test_handle_new_relationships_idempotent(
    conn, project_factory, memory_factory
):
    project = project_factory("ces5")
    a = memory_factory(project["id"], content="a")
    b = memory_factory(project["id"], content="b")

    edge = NewEdge(
        from_memory_id=a["id"],
        to_memory_id=b["id"],
        edge_type="depends_on",
    )
    handle_new_relationships(conn, project["id"], [edge])
    handle_new_relationships(conn, project["id"], [edge])  # idempotent

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory_dependencies "
            "WHERE from_memory_id = %s AND to_memory_id = %s",
            (a["id"], b["id"]),
        )
        assert cur.fetchone()[0] == 1


@pytest.mark.db
def test_handle_lesson_candidates_creates_lesson(
    conn, project_factory
):
    project = project_factory("ces6")

    handle_lesson_candidates(
        conn,
        project["id"],
        [
            LessonCandidate(
                title="Use prepared statements",
                content="Always use prepared statements with user input.",
                applies_when={"language": "sql"},
                compliance_profiles=["sox"],
            )
        ],
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT title, content, tier, strength, applies_when "
            "FROM devbrain.memory WHERE project_id = %s AND tier = 'lesson'",
            (project["id"],),
        )
        row = cur.fetchone()
    assert row is not None
    title, content, tier, strength, applies_when = row
    assert title == "Use prepared statements"
    assert "prepared statements" in content
    assert tier == "lesson"
    assert float(strength) == pytest.approx(1.0, abs=0.01)
    assert applies_when == {"language": "sql"}


@pytest.mark.db
def test_end_session_idempotent_handler_returns_status(
    conn, project_factory, memory_factory
):
    project = project_factory("ces7")
    m = memory_factory(project["id"], content="x")

    payload = {
        "session_id": "ses-int-1",
        "cascade_decisions": [
            {
                "memory_id": str(m["id"]),
                "action": "promote",
                "rationale": "",
            }
        ],
    }

    result = end_session_idempotent_handler(conn, project["id"], payload)
    assert result["status"] == "applied"
    assert "cascades_drained" in result


@pytest.mark.db
def test_end_session_drains_queue_after_applying_judgment(
    conn, project_factory, memory_factory
):
    """Phase 5e-NEW-2: end_session drains the cascade queue after judgment.

    Setup: a (depends_on) b. Pre-enqueue a queue row pointing at a as the
    dependent of b. Fire end_session. Assert: queue empty, a's strength
    dropped, last_cascade_at set, result reports cascades_drained > 0.
    """
    project = project_factory("ces8")
    src = memory_factory(project["id"], content="source")
    dep = memory_factory(project["id"], content="dependent")

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET strength = 0.9 WHERE id = %s",
            (dep["id"],),
        )
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'test')",
            (dep["id"], src["id"]),
        )
        cur.execute(
            "INSERT INTO devbrain.curator_re_eval_queue "
            "(memory_id, cascade_source_id, edge_type) "
            "VALUES (%s, %s, 'supersedes')",
            (dep["id"], src["id"]),
        )
    conn.commit()

    payload = {
        "session_id": "ses-drain-1",
        # No judgment payload — just exercising the drain trigger.
        "cascade_decisions": [],
        "new_relationships": [],
        "lesson_candidates": [],
    }
    result = end_session_idempotent_handler(conn, project["id"], payload)

    assert result["cascades_drained"] >= 1

    # Queue empty.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.curator_re_eval_queue "
            "WHERE memory_id = %s",
            (dep["id"],),
        )
        assert cur.fetchone()[0] == 0

    # Strength dropped, last_cascade_at set.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT strength, last_cascade_at FROM devbrain.memory "
            "WHERE id = %s",
            (dep["id"],),
        )
        strength, last_cascade_at = cur.fetchone()
    assert float(strength) < 0.9
    assert last_cascade_at is not None


@pytest.mark.db
def test_end_session_idempotent_repeat_returns_same_result(
    conn, project_factory, memory_factory
):
    project = project_factory("ces9")
    m = memory_factory(project["id"], content="x")
    payload = {
        "session_id": "ses-rep-1",
        "cascade_decisions": [
            {
                "memory_id": str(m["id"]),
                "action": "promote",
                "rationale": "",
            }
        ],
    }
    r1 = end_session_idempotent_handler(conn, project["id"], payload)
    r2 = end_session_idempotent_handler(conn, project["id"], payload)
    assert r1 == r2
