"""Three-signal feedback loop for lesson graduation + rule demotion.

Three signals fire after every REVIEWING phase, derived from comparing the
curator brief (which memories were surfaced) against the eval results
(which violations the eval agents found):

  Signal #1 — in-brief AND eval found a violation:
              hit_count++, current_streak = 0
              The memory was surfaced but the planner/implementer ignored
              it, so streak resets.

  Signal #2 — NOT in-brief but eval found a relevant violation:
              queue refinement (widen applies_when so the memory surfaces
              next time). Implementation lands in Phase 6d.

  Signal #3 — in-brief AND no violation (clean run):
              effective_hit_count++, current_streak++, last_hit = NOW()
              If tier='lesson' AND streak >= GRADUATION_STREAK_THRESHOLD,
              promote tier='lesson' -> tier='rule' (graduation).

A separate sweep (`demote_low_precision_rules`) runs periodically and
demotes any tier='rule' whose precision dropped below 0.50 over the last
30 days back to tier='lesson'.

All tier transitions are recorded in devbrain.memory_ledger automatically
via the AFTER UPDATE trigger from migration 015.

Public API:
- apply_feedback_signals(conn, job_id, brief, eval_results)
- demote_low_precision_rules(conn, project_id)
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Tunable constants — change here if real-world data shows them wrong.
GRADUATION_STREAK_THRESHOLD = 3
GRADUATION_FRESHNESS_WINDOW = "90 days"
DEMOTION_PRECISION_THRESHOLD = 0.50
DEMOTION_WINDOW = "30 days"


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
    from curator.refinement import queue_refinement
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
    """In-brief AND failure — reset streak, increment hit_count.

    The memory was surfaced in the brief but the eval agent still found a
    violation that maps back to it. The streak resets so a future
    graduation attempt has to re-prove three consecutive clean runs.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory "
            "SET hit_count = hit_count + 1, current_streak = 0 "
            "WHERE id = %s",
            (memory_id,),
        )
    conn.commit()


def _signal_success(conn, memory_id):
    """In-brief AND clean — streak++, effective_hit_count++, check graduation.

    A returning RETURNING clause gives us the post-update tier + streak in
    one round-trip so the graduation check doesn't need a second SELECT.
    If the row is missing (e.g. archived between brief generation and the
    feedback pass), fail closed and skip.
    """
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
    """Promote tier='lesson' to tier='rule'.

    The WHERE filter on tier='lesson' makes this idempotent — running it
    on an already-graduated row is a no-op. The AFTER UPDATE trigger
    (migration 015) writes the audit row to devbrain.memory_ledger.
    """
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
    untouched — we only demote rules that have been recently active and
    misfiring.

    The demotion sets tier back to 'lesson', stamps demoted_at, and resets
    current_streak so the row has to re-earn graduation. Project scope is
    enforced by project_id so the sweep can't accidentally demote rules
    from other projects.
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
    """Extract every memory ID referenced in a curator brief.

    `brief` may be either a CuratorBrief Pydantic model (returned by
    generate_brief) or a plain dict (loaded from
    factory_jobs.curator_brief JSONB). Walk rules + lessons +
    relevant_decisions and return a set of UUIDs.
    """
    if hasattr(brief, "model_dump"):
        brief = brief.model_dump()
    ids: set = set()
    for section in ("rules", "lessons", "relevant_decisions"):
        for ref in brief.get(section, []) or []:
            if isinstance(ref, dict):
                ids.add(ref["id"])
            else:
                # MemoryRef-like object (e.g. partially-decoded test input)
                ids.add(ref.id)
    return ids


def _index_findings_by_memory(eval_results):
    """Return {memory_id: [finding, ...]} for findings with a relevant_memory_id.

    Used to test "is this brief memory pointed at by any finding?" in O(1).
    Findings without a relevant_memory_id (rule_id is None heuristic
    findings) don't need to be indexed because they can't fire signal #1.
    """
    index: dict = {}
    for result in eval_results:
        for finding in result.findings:
            mid = finding.relevant_memory_id
            if mid is not None:
                index.setdefault(mid, []).append(finding)
    return index
