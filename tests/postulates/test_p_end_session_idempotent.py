"""P_end_session_idempotent — same session calling end_session() twice = same state.

POSTULATE
---------
Two end_session() calls with the same session_id and identical payloads
produce identical observable state. The second call returns the first
call's result without re-applying side-effects.

Different payload under the same session_id is treated as a NEW
application — different (session_id, payload_hash) PK row, fresh side
effects. That covers the "agent corrected its judgment and resubmitted"
case.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make `from curator.end_session import ...` resolve from tests/postulates/.
_FACTORY = Path(__file__).resolve().parents[2] / "factory"
if str(_FACTORY) not in sys.path:
    sys.path.insert(0, str(_FACTORY))

from curator.end_session import end_session_idempotent_handler  # noqa: E402


def test_idempotency_via_session_id_key(
    conn, project_factory, memory_factory
):
    project = project_factory("idem")
    m = memory_factory(project["id"], content="x")

    payload = {
        "session_id": "test-session-123",
        "cascade_decisions": [
            {
                "memory_id": str(m["id"]),
                "action": "promote",
                "rationale": "",
            }
        ],
        "new_relationships": [],
        "lesson_candidates": [],
    }

    r1 = end_session_idempotent_handler(conn, project["id"], payload)
    r2 = end_session_idempotent_handler(conn, project["id"], payload)
    assert r1 == r2
    assert r1["status"] == "applied"

    # Promotion should only have happened once — but tier is now 'lesson'
    # either way (idempotent UPDATE is a no-op the second time because of
    # the `tier = 'memory'` guard, but the meaningful assertion is that
    # the second call's RESULT matches the first AND the state matches
    # what one application would produce).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tier FROM devbrain.memory WHERE id = %s",
            (m["id"],),
        )
        assert cur.fetchone()[0] == "lesson"

    # Exactly ONE row in end_session_log for this session.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.end_session_log "
            "WHERE session_id = %s",
            (payload["session_id"],),
        )
        assert cur.fetchone()[0] == 1


def test_different_payload_same_session_id_is_new_application(
    conn, project_factory, memory_factory
):
    """Same session_id with corrected payload = new (session_id, hash) row,
    fresh side-effects. Models the 'agent corrected its judgment and
    resubmitted' workflow."""
    project = project_factory("idem2")
    m1 = memory_factory(project["id"], content="m1")
    m2 = memory_factory(project["id"], content="m2")

    payload_a = {
        "session_id": "edit-session",
        "cascade_decisions": [
            {"memory_id": str(m1["id"]), "action": "promote", "rationale": ""}
        ],
    }
    payload_b = {
        "session_id": "edit-session",  # SAME session_id
        "cascade_decisions": [
            {"memory_id": str(m2["id"]), "action": "promote", "rationale": ""}
        ],
    }

    end_session_idempotent_handler(conn, project["id"], payload_a)
    end_session_idempotent_handler(conn, project["id"], payload_b)

    # Both promoted.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, tier FROM devbrain.memory "
            "WHERE id = ANY(%s) ORDER BY content",
            ([m1["id"], m2["id"]],),
        )
        rows = cur.fetchall()
    tiers = {r[0]: r[1] for r in rows}
    assert tiers[m1["id"]] == "lesson"
    assert tiers[m2["id"]] == "lesson"

    # Two rows in end_session_log — one per payload hash.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.end_session_log "
            "WHERE session_id = %s",
            ("edit-session",),
        )
        assert cur.fetchone()[0] == 2
