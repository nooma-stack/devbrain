"""P_cognify_strengthen_unchanged: graduation/demotion semantics from Step 6c
are preserved bit-for-bit after the file move to factory/cognify/strengthen.py.

Verifies that:
  1. curator.graduation re-exports the exact same objects as cognify.strengthen
  2. Key behaviour (graduation threshold, demotion threshold) is identical
  3. The shim is not a copy — it's a re-export (same function objects)
"""
from __future__ import annotations

import pytest

import cognify.strengthen as cs
import curator.graduation as cg


def test_strengthen_constants_match_shim():
    """Constants in cognify.strengthen match those re-exported via curator.graduation."""
    assert cs.GRADUATION_STREAK_THRESHOLD == cg.GRADUATION_STREAK_THRESHOLD
    assert cs.GRADUATION_FRESHNESS_WINDOW == cg.GRADUATION_FRESHNESS_WINDOW
    assert cs.DEMOTION_PRECISION_THRESHOLD == cg.DEMOTION_PRECISION_THRESHOLD
    assert cs.DEMOTION_WINDOW == cg.DEMOTION_WINDOW


def test_strengthen_functions_are_same_objects_as_shim():
    """curator.graduation re-exports the same function objects from cognify.strengthen.

    This is a structural check: the shim must import-and-re-export, not copy.
    """
    assert cg.apply_feedback_signals is cs.apply_feedback_signals
    assert cg.demote_low_precision_rules is cs.demote_low_precision_rules
    assert cg._signal_failure is cs._signal_failure
    assert cg._signal_success is cs._signal_success
    assert cg._graduate is cs._graduate
    assert cg._collect_brief_memory_ids is cs._collect_brief_memory_ids
    assert cg._index_findings_by_memory is cs._index_findings_by_memory


@pytest.mark.db
def test_graduation_threshold_unchanged(conn, project_factory, memory_factory):
    """Graduation threshold is still 3 consecutive successes after the move."""
    from cognify.strengthen import (
        GRADUATION_STREAK_THRESHOLD,
        _signal_success,
    )

    project = project_factory("p_str_thresh")
    m = memory_factory(project["id"], tier="lesson")

    # Seed streak = threshold - 1
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET current_streak = %s WHERE id = %s",
            (GRADUATION_STREAK_THRESHOLD - 1, m["id"]),
        )
    conn.commit()

    _signal_success(conn, m["id"])

    with conn.cursor() as cur:
        cur.execute("SELECT tier FROM devbrain.memory WHERE id = %s", (m["id"],))
        tier = cur.fetchone()[0]

    assert tier == "rule", (
        f"Expected graduation at streak={GRADUATION_STREAK_THRESHOLD} "
        f"but tier is still {tier!r}"
    )


@pytest.mark.db
def test_demotion_threshold_unchanged(conn, project_factory, memory_factory):
    """Demotion precision threshold is still 0.50 after the move."""
    from cognify.strengthen import (
        DEMOTION_PRECISION_THRESHOLD,
        demote_low_precision_rules,
    )

    project = project_factory("p_str_demote")
    rule = memory_factory(project["id"], tier="rule")

    # Precision = effective / (hit + effective) = 1/6 ≈ 0.167 < 0.5
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory "
            "SET hit_count = 5, effective_hit_count = 1, "
            "    last_hit = NOW() - INTERVAL '1 day' "
            "WHERE id = %s",
            (rule["id"],),
        )
    conn.commit()

    demote_low_precision_rules(conn, project["id"])

    with conn.cursor() as cur:
        cur.execute("SELECT tier FROM devbrain.memory WHERE id = %s", (rule["id"],))
        tier = cur.fetchone()[0]

    assert tier == "lesson"
