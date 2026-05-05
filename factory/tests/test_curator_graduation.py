"""Unit tests for the three-signal graduation pipeline + demote sweep.

Covers factory/curator/graduation.py:
  * _signal_failure resets streak, increments hit_count
  * _signal_success increments streak + effective_hit_count + last_hit
  * _signal_success triggers graduation at streak == 3 for lessons
  * _signal_success does NOT graduate at streak < 3 or for non-lesson tiers
  * _graduate is idempotent on already-graduated rows
  * apply_feedback_signals classifies in-brief findings into signals 1 vs 3
  * demote_low_precision_rules respects precision threshold + freshness window
  * demote_low_precision_rules is project-scoped

These tests exercise the real DB substrate (current_streak from migration
019, effective_hit_count from migration 020, AFTER trigger from migration
015 writing memory_ledger rows on tier transitions).

Signal #2 (refinement) is NOT exercised here — that ships in Phase 6d
when curator.refinement.queue_refinement is implemented. The graduation
module already swallows the NotImplementedError so apply_feedback_signals
exercises signals #1 and #3 cleanly even with refinement stubbed out.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from curator.eval.types import EvalFinding, EvalResult
from curator.graduation import (
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


def _seed_counters(
    conn,
    memory_id,
    *,
    current_streak: int = 0,
    hit_count: int = 0,
    effective_hit_count: int = 0,
    last_hit_offset_days: float | None = None,
):
    """Set graduation-related counters directly via UPDATE.

    The memory_factory doesn't take these kwargs (Phase 6a only added the
    schema columns); tests need to seed them after insert.
    """
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


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers (no DB)
# ─────────────────────────────────────────────────────────────────────────────


def test_collect_brief_memory_ids_handles_dict_brief():
    """Plain dict (the JSONB shape) walks all three sections."""
    a, b, c = uuid4(), uuid4(), uuid4()
    brief = {
        "rules": [{"id": a}],
        "lessons": [{"id": b}],
        "relevant_decisions": [{"id": c}],
    }
    assert _collect_brief_memory_ids(brief) == {a, b, c}


def test_collect_brief_memory_ids_handles_pydantic_brief():
    """CuratorBrief Pydantic model is converted via model_dump."""
    from decimal import Decimal

    from curator.types import CuratorBrief, MemoryRef

    a = uuid4()
    ref = MemoryRef(
        id=a,
        kind="decision",
        title="t",
        content_excerpt="x",
        tier="rule",
        strength=Decimal("1.0"),
        last_cascade_at=None,
    )
    brief = CuratorBrief(
        version="1.0",
        job_id=uuid4(),
        project_id=uuid4(),
        rules=[ref],
        lessons=[],
        relevant_decisions=[],
        recent_cascade_signals=[],
        generated_at=datetime.now(timezone.utc),
    )
    assert _collect_brief_memory_ids(brief) == {a}


def test_collect_brief_memory_ids_handles_empty_sections():
    """Missing or None sections don't blow up."""
    assert _collect_brief_memory_ids({}) == set()
    assert _collect_brief_memory_ids(
        {"rules": None, "lessons": [], "relevant_decisions": []}
    ) == set()


def test_collect_brief_memory_ids_handles_object_refs_in_dict_brief():
    """Mixed-shape brief: a dict whose section contains object-style refs
    (e.g. from a partially-decoded test or a future serializer that
    preserves Pydantic objects). Falls through to the .id attribute path.
    """
    class _Ref:
        def __init__(self, mid):
            self.id = mid

    a = uuid4()
    brief = {"rules": [_Ref(a)], "lessons": [], "relevant_decisions": []}
    assert _collect_brief_memory_ids(brief) == {a}


def test_index_findings_by_memory_skips_findings_with_null_memory_id():
    finding_with = EvalFinding(
        rule_id=uuid4(),
        severity="critical",
        file="x.py",
        line=1,
        message="m",
        fix_hint="h",
        relevant_memory_id=uuid4(),
    )
    finding_without = EvalFinding(
        rule_id=None,
        severity="minor",
        file="y.py",
        line=2,
        message="m2",
        fix_hint="h2",
        relevant_memory_id=None,
    )
    result = EvalResult(
        version="1.0",
        job_id=uuid4(),
        agent_name="eval_security",
        findings=[finding_with, finding_without],
        elapsed_ms=1,
        started_at=datetime.now(timezone.utc),
    )
    index = _index_findings_by_memory([result])
    assert finding_with.relevant_memory_id in index
    assert len(index) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Signal handlers (DB)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.db
def test_signal_failure_resets_streak_and_increments_hit_count(
    conn, project_factory, memory_factory
):
    project = project_factory("sigfail")
    m = memory_factory(project["id"], tier="lesson")
    _seed_counters(
        conn, m["id"], current_streak=2, hit_count=4, effective_hit_count=7
    )

    _signal_failure(conn, m["id"])

    after = _read_counters(conn, m["id"])
    assert after["current_streak"] == 0
    assert after["hit_count"] == 5
    # effective_hit_count + last_hit untouched by failure signal
    assert after["effective_hit_count"] == 7
    assert after["last_hit"] is None
    # tier didn't change
    assert after["tier"] == "lesson"


@pytest.mark.db
def test_signal_success_increments_streak_and_effective_hit_count(
    conn, project_factory, memory_factory
):
    project = project_factory("sigsucc")
    m = memory_factory(project["id"], tier="lesson")
    _seed_counters(
        conn, m["id"], current_streak=0, hit_count=3, effective_hit_count=5
    )

    _signal_success(conn, m["id"])

    after = _read_counters(conn, m["id"])
    assert after["current_streak"] == 1
    assert after["effective_hit_count"] == 6
    assert after["last_hit"] is not None
    # hit_count untouched by success signal
    assert after["hit_count"] == 3
    # streak hasn't hit threshold yet
    assert after["tier"] == "lesson"
    assert after["graduated_at"] is None


@pytest.mark.db
def test_signal_success_graduates_lesson_at_threshold(
    conn, project_factory, memory_factory
):
    project = project_factory("siggrad")
    m = memory_factory(project["id"], tier="lesson")
    # Pre-seed streak = THRESHOLD - 1; one more success triggers graduation.
    _seed_counters(
        conn, m["id"], current_streak=GRADUATION_STREAK_THRESHOLD - 1
    )

    _signal_success(conn, m["id"])

    after = _read_counters(conn, m["id"])
    assert after["tier"] == "rule"
    assert after["current_streak"] == GRADUATION_STREAK_THRESHOLD
    assert after["graduated_at"] is not None


@pytest.mark.db
def test_signal_success_does_not_graduate_below_threshold(
    conn, project_factory, memory_factory
):
    """At streak = THRESHOLD - 2, one success leaves us at THRESHOLD - 1
    which is below the gate, so no graduation should fire."""
    project = project_factory("signograd")
    m = memory_factory(project["id"], tier="lesson")
    _seed_counters(
        conn, m["id"], current_streak=GRADUATION_STREAK_THRESHOLD - 2
    )

    _signal_success(conn, m["id"])

    after = _read_counters(conn, m["id"])
    assert after["tier"] == "lesson"
    assert after["graduated_at"] is None
    assert after["current_streak"] == GRADUATION_STREAK_THRESHOLD - 1


@pytest.mark.db
def test_signal_success_does_not_graduate_non_lesson_tiers(
    conn, project_factory, memory_factory
):
    """tier='rule' and tier='memory' are NOT eligible for graduation even
    at streak >= threshold."""
    project = project_factory("signonlesson")
    rule = memory_factory(project["id"], tier="rule")
    mem = memory_factory(project["id"], tier="memory")
    _seed_counters(
        conn, rule["id"], current_streak=GRADUATION_STREAK_THRESHOLD - 1
    )
    _seed_counters(
        conn, mem["id"], current_streak=GRADUATION_STREAK_THRESHOLD - 1
    )

    _signal_success(conn, rule["id"])
    _signal_success(conn, mem["id"])

    rule_after = _read_counters(conn, rule["id"])
    mem_after = _read_counters(conn, mem["id"])
    assert rule_after["tier"] == "rule"
    assert rule_after["graduated_at"] is None
    assert mem_after["tier"] == "memory"
    assert mem_after["graduated_at"] is None


@pytest.mark.db
def test_signal_success_no_op_on_missing_row(
    conn, project_factory, memory_factory
):
    """If the memory was archived between brief generation and feedback,
    the UPDATE...RETURNING returns no rows; no-op (don't crash)."""
    fake_id = uuid4()
    # Should not raise even though no row matches.
    _signal_success(conn, fake_id)


# ─────────────────────────────────────────────────────────────────────────────
# _graduate
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.db
def test_graduate_sets_tier_and_graduated_at(
    conn, project_factory, memory_factory
):
    project = project_factory("grad")
    m = memory_factory(project["id"], tier="lesson")

    _graduate(conn, m["id"])

    after = _read_counters(conn, m["id"])
    assert after["tier"] == "rule"
    assert after["graduated_at"] is not None


@pytest.mark.db
def test_graduate_is_idempotent_on_already_graduated_row(
    conn, project_factory, memory_factory
):
    """Running _graduate twice is a no-op — the WHERE clause filters on
    tier='lesson' so the second call updates nothing."""
    project = project_factory("gradidem")
    m = memory_factory(project["id"], tier="lesson")

    _graduate(conn, m["id"])
    after_first = _read_counters(conn, m["id"])
    first_grad_at = after_first["graduated_at"]

    # Second call must not change graduated_at (no UPDATE fires).
    _graduate(conn, m["id"])
    after_second = _read_counters(conn, m["id"])
    assert after_second["tier"] == "rule"
    assert after_second["graduated_at"] == first_grad_at


# ─────────────────────────────────────────────────────────────────────────────
# apply_feedback_signals
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.db
def test_apply_feedback_signals_classifies_in_brief_findings(
    conn, project_factory, memory_factory
):
    """Walks brief, fires signal #1 for in-brief memories with findings,
    signal #3 for in-brief memories without findings."""
    project = project_factory("apply")
    fired = memory_factory(project["id"], tier="lesson", content="failed")
    clean = memory_factory(project["id"], tier="lesson", content="clean")
    _seed_counters(conn, fired["id"], current_streak=2, hit_count=0)
    _seed_counters(conn, clean["id"], current_streak=0, hit_count=0)

    brief = {
        "rules": [],
        "lessons": [
            {"id": fired["id"]},
            {"id": clean["id"]},
        ],
        "relevant_decisions": [],
    }

    finding_for_fired = EvalFinding(
        rule_id=fired["id"],
        severity="important",
        file="x.py",
        line=10,
        message="violation",
        fix_hint="...",
        relevant_memory_id=fired["id"],
    )
    eval_result = EvalResult(
        version="1.0",
        job_id=uuid4(),
        agent_name="eval_security",
        findings=[finding_for_fired],
        elapsed_ms=10,
        started_at=datetime.now(timezone.utc),
    )

    apply_feedback_signals(conn, uuid4(), brief, [eval_result])

    fired_after = _read_counters(conn, fired["id"])
    clean_after = _read_counters(conn, clean["id"])

    # fired memory: signal #1 (failure)
    assert fired_after["current_streak"] == 0
    assert fired_after["hit_count"] == 1

    # clean memory: signal #3 (success)
    assert clean_after["current_streak"] == 1
    assert clean_after["effective_hit_count"] == 1
    assert clean_after["last_hit"] is not None


@pytest.mark.db
def test_apply_feedback_signals_swallows_signal2_not_implemented(
    conn, project_factory, memory_factory
):
    """A finding pointing at a memory NOT in the brief tries to call
    queue_refinement, which raises NotImplementedError until Phase 6d.
    The swallow-and-log path lets signals #1 and #3 still fire."""
    project = project_factory("sig2swallow")
    in_brief = memory_factory(project["id"], tier="lesson", content="in brief")
    not_in_brief = memory_factory(
        project["id"], tier="lesson", content="missing from brief"
    )
    _seed_counters(conn, in_brief["id"], current_streak=0)

    brief = {
        "rules": [],
        "lessons": [{"id": in_brief["id"]}],
        "relevant_decisions": [],
    }

    # Finding points at a memory that's NOT in the brief — signal #2 path.
    finding = EvalFinding(
        rule_id=not_in_brief["id"],
        severity="minor",
        file="x.py",
        line=1,
        message="missed",
        fix_hint="...",
        relevant_memory_id=not_in_brief["id"],
    )
    eval_result = EvalResult(
        version="1.0",
        job_id=uuid4(),
        agent_name="eval_test",
        findings=[finding],
        elapsed_ms=5,
        started_at=datetime.now(timezone.utc),
    )

    # Should NOT raise — refinement stub's NotImplementedError is swallowed.
    apply_feedback_signals(conn, uuid4(), brief, [eval_result])

    # Signal #3 still fired for the in-brief memory.
    after = _read_counters(conn, in_brief["id"])
    assert after["current_streak"] == 1
    assert after["effective_hit_count"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# demote_low_precision_rules
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.db
def test_demote_demotes_rules_below_precision_threshold(
    conn, project_factory, memory_factory
):
    """A rule with hit_count=5, effective_hit_count=3 (precision = 0.375 <
    0.5) and a recent last_hit gets demoted to lesson."""
    project = project_factory("demote")
    rule = memory_factory(project["id"], tier="rule")
    _seed_counters(
        conn,
        rule["id"],
        current_streak=4,
        hit_count=5,
        effective_hit_count=3,
        last_hit_offset_days=1,
    )

    demote_low_precision_rules(conn, project["id"])

    after = _read_counters(conn, rule["id"])
    assert after["tier"] == "lesson"
    assert after["demoted_at"] is not None
    assert after["current_streak"] == 0


@pytest.mark.db
def test_demote_ignores_rules_above_precision_threshold(
    conn, project_factory, memory_factory
):
    """A rule with hit_count=2, effective_hit_count=8 (precision = 0.8 >=
    0.5) is left alone."""
    project = project_factory("nodemote")
    rule = memory_factory(project["id"], tier="rule")
    _seed_counters(
        conn,
        rule["id"],
        hit_count=2,
        effective_hit_count=8,
        last_hit_offset_days=1,
    )

    demote_low_precision_rules(conn, project["id"])

    after = _read_counters(conn, rule["id"])
    assert after["tier"] == "rule"
    assert after["demoted_at"] is None


@pytest.mark.db
def test_demote_ignores_stale_rules(
    conn, project_factory, memory_factory
):
    """A rule with low precision but last_hit older than the demotion
    window is NOT demoted — we only demote recently-active misfiring rules.
    """
    project = project_factory("stale")
    rule = memory_factory(project["id"], tier="rule")
    _seed_counters(
        conn,
        rule["id"],
        hit_count=10,
        effective_hit_count=1,
        last_hit_offset_days=60,  # well past the 30-day window
    )

    demote_low_precision_rules(conn, project["id"])

    after = _read_counters(conn, rule["id"])
    assert after["tier"] == "rule"
    assert after["demoted_at"] is None


@pytest.mark.db
def test_demote_resets_current_streak(
    conn, project_factory, memory_factory
):
    """Demotion zeros current_streak so the lesson has to re-earn graduation."""
    project = project_factory("demoteresets")
    rule = memory_factory(project["id"], tier="rule")
    _seed_counters(
        conn,
        rule["id"],
        current_streak=10,
        hit_count=5,
        effective_hit_count=1,
        last_hit_offset_days=1,
    )

    # Sanity: precision = 1/6 ≈ 0.167 < 0.5
    assert 1 / (5 + 1) < DEMOTION_PRECISION_THRESHOLD

    demote_low_precision_rules(conn, project["id"])

    after = _read_counters(conn, rule["id"])
    assert after["tier"] == "lesson"
    assert after["current_streak"] == 0


@pytest.mark.db
def test_demote_is_project_scoped(
    conn, project_factory, memory_factory
):
    """A demotion sweep on project A must NOT touch low-precision rules
    in project B."""
    project_a = project_factory("scopea")
    project_b = project_factory("scopeb")
    rule_a = memory_factory(project_a["id"], tier="rule")
    rule_b = memory_factory(project_b["id"], tier="rule")
    for r in (rule_a, rule_b):
        _seed_counters(
            conn,
            r["id"],
            hit_count=5,
            effective_hit_count=1,
            last_hit_offset_days=1,
        )

    demote_low_precision_rules(conn, project_a["id"])

    after_a = _read_counters(conn, rule_a["id"])
    after_b = _read_counters(conn, rule_b["id"])
    assert after_a["tier"] == "lesson"
    assert after_b["tier"] == "rule"
    assert after_b["demoted_at"] is None
