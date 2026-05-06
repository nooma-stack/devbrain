"""P_cognify_decay_monotonic: decay only decreases strength; never increases."""
from __future__ import annotations

import pytest

from cognify.decay import DecayPass


def _set_strength(conn, memory_id, strength):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET strength = %s WHERE id = %s",
            (strength, memory_id),
        )
    conn.commit()


def _set_idle(conn, memory_id, days):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory "
            "SET last_cascade_at = NOW() - INTERVAL '" + str(days) + " days', "
            "    last_hit = NOW() - INTERVAL '" + str(days) + " days', "
            "    created_at = NOW() - INTERVAL '" + str(days) + " days' "
            "WHERE id = %s",
            (memory_id,),
        )
    conn.commit()


def _read_strength(conn, memory_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT strength FROM devbrain.memory WHERE id = %s",
            (memory_id,),
        )
        return float(cur.fetchone()[0])


@pytest.mark.db
def test_p_cognify_decay_monotonic(conn, project_factory, memory_factory):
    """Decay never increases strength. Running decay multiple times is safe."""
    project = project_factory("p_decay_mono")
    m = memory_factory(project["id"])
    initial_strength = 0.9
    _set_strength(conn, m["id"], initial_strength)
    _set_idle(conn, m["id"], 45)

    pass_ = DecayPass()

    # Run decay 3 times.
    strengths = [initial_strength]
    for _ in range(3):
        pass_.run(conn, project["id"])
        strengths.append(_read_strength(conn, m["id"]))

    # Each application must be <= the previous.
    for i in range(1, len(strengths)):
        assert strengths[i] <= strengths[i - 1], (
            f"Decay increased strength at step {i}: "
            f"{strengths[i - 1]} -> {strengths[i]}"
        )

    # Final strength must be strictly less than initial (decay actually ran).
    assert strengths[-1] < initial_strength
