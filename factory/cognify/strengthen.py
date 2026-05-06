"""cognify_strengthen — lesson graduation and rule demotion.

This module is the graduated home for the graduation pipeline from
factory/curator/graduation.py. The logic is identical — this is a pure
relocation. factory/curator/graduation.py now imports from here and
re-exports the public API for backwards compatibility with existing callers
(state_machine.py, tests).

Public API (unchanged from graduation.py):
  apply_feedback_signals(conn, job_id, brief, eval_results)
  demote_low_precision_rules(conn, project_id)

Additional export (new, for the cognify orchestrator):
  run_strengthen_pass(conn, project_id) → PassResult

Three-signal feedback loop (unchanged):
  Signal #1 — in-brief AND eval found a violation → hit_count++, streak=0
  Signal #2 — NOT in-brief but eval found a violation → queue refinement
  Signal #3 — in-brief AND no violation → effective_hit_count++, streak++
               If tier='lesson' AND streak >= 3 → graduate to tier='rule'

See factory/curator/graduation.py docstring for full design notes.
"""
from __future__ import annotations

import logging

from cognify.orchestrator import CognifyPass, PassResult, register_pass

logger = logging.getLogger(__name__)

# Tunable constants — match graduation.py exactly (behaviour preservation).
GRADUATION_STREAK_THRESHOLD = 3
GRADUATION_FRESHNESS_WINDOW = "90 days"
DEMOTION_PRECISION_THRESHOLD = 0.50
DEMOTION_WINDOW = "30 days"


# ─────────────────────────────────────────────────────────────────────────────
# Public pass entrypoint (cognify orchestrator)
# ─────────────────────────────────────────────────────────────────────────────


@register_pass
class StrengthenPass(CognifyPass):
    """cognify_strengthen: run graduation + demotion sweep.

    Daily cadence. Zero LLM cost (uses precision tracking only).
    """

    pass_name = "strengthen"

    def run(self, conn, project_id, *, dry_run: bool = False) -> PassResult:
        if project_id is None:
            raise ValueError(
                "cognify_strengthen requires a project_id"
            )
        if dry_run:
            return PassResult(
                rows_processed=0,
                llm_calls=0,
                metadata={"pass": "strengthen", "dry_run": True},
            )
        demote_low_precision_rules(conn, project_id)
        return PassResult(
            rows_processed=0,
            llm_calls=0,
            metadata={"pass": "strengthen"},
        )


def run_strengthen_pass(conn, project_id) -> PassResult:
    """Convenience wrapper: run the strengthen pass for a project."""
    return StrengthenPass().run(conn, project_id)


# ─────────────────────────────────────────────────────────────────────────────
# Core graduation logic (moved verbatim from factory/curator/graduation.py)
# ─────────────────────────────────────────────────────────────────────────────


def apply_feedback_signals(conn, job_id, brief, eval_results):
    """Apply the three feedback signals based on a brief + eval results.

    For each memory in the brief:
      - if any finding's relevant_memory_id matches it -> signal #1 (failure)
      - else -> signal #3 (success), may trigger graduation

    For each finding whose relevant_memory_id is NOT in the brief:
      -> signal #2 (refinement) — queue for applies_when widening

    Args:
        conn: psycopg2 connection (transaction managed inside helpers).
        job_id: factory_jobs.id (currently unused but reserved for future
            audit logging that wants to correlate signals to a specific job).
        brief: CuratorBrief Pydantic model OR a dict (loaded from JSONB).
            Walked for memory IDs across rules + lessons + relevant_decisions.
        eval_results: list[EvalResult] — output of curator.eval.runner.run_evals.
    """
    in_brief_ids = _collect_brief_memory_ids(brief)
    findings_by_memory = _index_findings_by_memory(eval_results)

    # Signals #1 and #3: walk in-brief memory IDs, classify by whether
    # any finding's relevant_memory_id points at them.
    for mid in in_brief_ids:
        if mid in findings_by_memory:
            _signal_failure(conn, mid)
        else:
            _signal_success(conn, mid)

    # Signal #2: findings with relevant_memory_id NOT in the brief —
    # queue for applies_when refinement so they surface next time.
    # The refinement helper raises NotImplementedError until Phase 6d
    # ships; swallow that here so the failure/success signals still
    # apply when the runner is invoked from a partially-implemented
    # state machine (Phase 6c).
    from cognify.refine import queue_refinement
    for result in eval_results:
        for finding in result.findings:
            mid = finding.relevant_memory_id
            if mid is not None and mid not in in_brief_ids:
                try:
                    queue_refinement(conn, finding)
                except NotImplementedError:
                    logger.debug(
                        "queue_refinement not implemented yet; deferring "
                        "signal #2 for finding %s -> memory %s",
                        finding.message,
                        mid,
                    )


def _signal_failure(conn, memory_id):
    """In-brief AND failure — reset streak, increment hit_count."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory "
            "SET hit_count = hit_count + 1, current_streak = 0 "
            "WHERE id = %s",
            (memory_id,),
        )
    conn.commit()


def _signal_success(conn, memory_id):
    """In-brief AND clean — streak++, effective_hit_count++, check graduation."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory "
            "SET effective_hit_count = effective_hit_count + 1, "
            "    current_streak = current_streak + 1, "
            "    last_hit = NOW() "
            "WHERE id = %s "
            "RETURNING tier, current_streak",
            (memory_id,),
        )
        row = cur.fetchone()
    conn.commit()
    if row is None:
        return
    tier, streak = row
    if tier == "lesson" and streak >= GRADUATION_STREAK_THRESHOLD:
        _graduate(conn, memory_id)


def _graduate(conn, memory_id):
    """Promote tier='lesson' to tier='rule'. Idempotent."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory "
            "SET tier = 'rule', graduated_at = NOW() "
            "WHERE id = %s AND tier = 'lesson'",
            (memory_id,),
        )
    conn.commit()


def demote_low_precision_rules(conn, project_id):
    """Sweep rules with precision < 50% over a 30-day window. Demote to lesson.

    Precision = effective_hit_count / (hit_count + effective_hit_count).
    Stale rules (last_hit older than the demotion window or NULL) are
    untouched. Project scope is enforced by project_id.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE devbrain.memory
            SET tier = 'lesson', demoted_at = NOW(), current_streak = 0
            WHERE tier = 'rule'
              AND project_id = %s
              AND last_hit > NOW() - INTERVAL '{DEMOTION_WINDOW}'
              AND CAST(effective_hit_count AS FLOAT)
                  / NULLIF(hit_count + effective_hit_count, 0)
                  < %s
            """,
            (project_id, DEMOTION_PRECISION_THRESHOLD),
        )
    conn.commit()


def _collect_brief_memory_ids(brief):
    """Extract every memory ID referenced in a curator brief."""
    if hasattr(brief, "model_dump"):
        brief = brief.model_dump()
    ids: set = set()
    for section in ("rules", "lessons", "relevant_decisions"):
        for ref in brief.get(section, []) or []:
            if isinstance(ref, dict):
                ids.add(ref["id"])
            else:
                ids.add(ref.id)
    return ids


def _index_findings_by_memory(eval_results):
    """Return {memory_id: [finding, ...]} for findings with a relevant_memory_id."""
    index: dict = {}
    for result in eval_results:
        for finding in result.findings:
            mid = finding.relevant_memory_id
            if mid is not None:
                index.setdefault(mid, []).append(finding)
    return index
