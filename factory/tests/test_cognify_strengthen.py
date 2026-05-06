"""Tests for cognify_strengthen (graduation move).

Verifies that the logic moved from factory/curator/graduation.py to
factory/cognify/strengthen.py is preserved bit-for-bit.

These tests mirror test_curator_graduation.py structurally but import
from cognify.strengthen directly to exercise the new module location.
Both the shim (curator.graduation) and the real module (cognify.strengthen)
must produce identical behaviour — this is the P_cognify_strengthen_unchanged
postulate.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from cognify.strengthen import (
    DEMOTION_PRECISION_THRESHOLD,
    GRADUATION_STREAK_THRESHOLD,
    _collect_brief_memory_ids,
    _graduate,
    _index_findings_by_memory,
    _signal_failure,
    _signal_success,
    apply_feedback_signals,
    demote_low_precision_rules,
)
from curator.eval.types import EvalFinding, EvalResult


def _seed_counters(
    conn,
    memory_id,
    *,
    current_streak=0,
    hit_count=0,
    effective_hit_count=0,
    last_hit_offset_days=None,
):
    if last_hit_offset_days is None:
        last_hit_sql = "last_hit = NULL"
        params = (current_streak, hit_count, effective_hit_count, memory_id)
    else:
        last_hit_sql = (
            f"last_hit = NOW() - INTERVAL '{last_hit_offset_days} days'"
        )
        params = (current_streak, hit_count, effective_hit_count, memory_id)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE devbrain.memory SET "
            f"current_streak = %s, hit_count = %s, "
            f"effective_hit_count = %s, {last_hit_sql} "
            f"WHERE id = %s",
            params,
        )
    conn.commit()


def _read_counters(conn, memory_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tier, current_streak, hit_count, effective_hit_count, "
            "last_hit, graduated_at, demoted_at "
            "FROM devbrain.memory WHERE id = %s",
            (memory_id,),
        )
        row = cur.fetchone()
    return {
        "tier": row[0],
        "current_streak": row[1],
        "hit_count": row[2],
        "effective_hit_count": row[3],
        "last_hit": row[4],
        "graduated_at": row[5],
        "demoted_at": row[6],
    }


# ── Pure-Python helpers ───────────────────────────────────────────────────────


def test_collect_brief_memory_ids_via_strengthen():
    """Ensure _collect_brief_memory_ids works from cognify.strengthen."""
    a, b = uuid4(), uuid4()
    brief = {"rules": [{"id": a}], "lessons": [{"id": b}], "relevant_decisions": []}
    assert _collect_brief_memory_ids(brief) == {a, b}


def test_index_findings_by_memory_via_strengthen():
    """Ensure _index_findings_by_memory works from cognify.strengthen."""
    mid = uuid4()
    finding = EvalFinding(
        rule_id=mid,
        severity="critical",
        file="x.py",
        line=1,
        message="m",
        fix_hint="h",
        relevant_memory_id=mid,
    )
    result = EvalResult(
        version="1.0",
        job_id=uuid4(),
        agent_name="eval_security",
        findings=[finding],
        elapsed_ms=1,
        started_at=datetime.now(timezone.utc),
    )
    index = _index_findings_by_memory([result])
    assert mid in index


# ── DB tests ──────────────────────────────────────────────────────────────────


@pytest.mark.db
def test_signal_failure_via_strengthen(conn, project_factory, memory_factory):
    project = project_factory("str_sigfail")
    m = memory_factory(project["id"], tier="lesson")
    _seed_counters(conn, m["id"], current_streak=2, hit_count=4)

    _signal_failure(conn, m["id"])

    after = _read_counters(conn, m["id"])
    assert after["current_streak"] == 0
    assert after["hit_count"] == 5


@pytest.mark.db
def test_signal_success_via_strengthen(conn, project_factory, memory_factory):
    project = project_factory("str_sigsucc")
    m = memory_factory(project["id"], tier="lesson")
    _seed_counters(conn, m["id"], current_streak=0)

    _signal_success(conn, m["id"])

    after = _read_counters(conn, m["id"])
    assert after["current_streak"] == 1
    assert after["effective_hit_count"] == 1
    assert after["last_hit"] is not None


@pytest.mark.db
def test_graduate_via_strengthen(conn, project_factory, memory_factory):
    project = project_factory("str_grad")
    m = memory_factory(project["id"], tier="lesson")

    _graduate(conn, m["id"])

    after = _read_counters(conn, m["id"])
    assert after["tier"] == "rule"
    assert after["graduated_at"] is not None


@pytest.mark.db
def test_graduation_at_threshold_via_strengthen(conn, project_factory, memory_factory):
    project = project_factory("str_thresh")
    m = memory_factory(project["id"], tier="lesson")
    _seed_counters(conn, m["id"], current_streak=GRADUATION_STREAK_THRESHOLD - 1)

    _signal_success(conn, m["id"])

    after = _read_counters(conn, m["id"])
    assert after["tier"] == "rule"


@pytest.mark.db
def test_demote_via_strengthen(conn, project_factory, memory_factory):
    project = project_factory("str_demote")
    rule = memory_factory(project["id"], tier="rule")
    _seed_counters(
        conn,
        rule["id"],
        hit_count=5,
        effective_hit_count=1,
        last_hit_offset_days=1,
    )

    demote_low_precision_rules(conn, project["id"])

    after = _read_counters(conn, rule["id"])
    assert after["tier"] == "lesson"
    assert after["demoted_at"] is not None


@pytest.mark.db
def test_shim_matches_strengthen(conn, project_factory, memory_factory):
    """curator.graduation shim re-exports from cognify.strengthen correctly."""
    from curator.graduation import (
        GRADUATION_STREAK_THRESHOLD as GST_shim,
        apply_feedback_signals as afs_shim,
        demote_low_precision_rules as dlpr_shim,
    )
    from cognify.strengthen import (
        GRADUATION_STREAK_THRESHOLD as GST_real,
        apply_feedback_signals as afs_real,
        demote_low_precision_rules as dlpr_real,
    )

    assert GST_shim == GST_real
    assert afs_shim is afs_real
    assert dlpr_shim is dlpr_real
