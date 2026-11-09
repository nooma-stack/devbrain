"""cognify_decay — idle-time strength ceiling.

Strength records how recently a memory was used. A row nobody has touched
for a while is capped at a lower ceiling:

  idle >= 30 days: strength <= 0.50
  idle >= 90 days: strength <= 0.12

"Idle" is measured by GREATEST(last_cascade_at, last_hit, created_at).

The pass sets ``strength = LEAST(strength, ceiling)``, so it is idempotent:
running it every hour leaves a row exactly where the first run put it.
(Until 2026-11 it multiplied by 0.5 / 0.12 on every hourly run, which
drove any row idle 30+ days to the 0.001 floor within hours.)

Decay never archives or hides anything. Strength is not used to rank
search results, and no pass archives a memory because its strength is low.

SQL-only pass: zero LLM cost, runs hourly. Every UPDATE fires the
memory_ledger trigger (migration 015), so each change is audited.
"""

from __future__ import annotations

import logging
from typing import Any

from cognify.orchestrator import CognifyPass, PassResult, register_pass

logger = logging.getLogger(__name__)

# (idle days, strength ceiling), longest idle first. A row gets the ceiling
# of the longest tier it qualifies for.
DECAY_TIERS: tuple[tuple[int, float], ...] = ((90, 0.12), (30, 0.50))
DECAY_30D_CAP = 0.50
DECAY_90D_CAP = 0.12

IDLE_EXPR = "GREATEST(last_cascade_at, last_hit, created_at)"


@register_pass
class DecayPass(CognifyPass):
    """cognify_decay: cap the strength of idle rows. Idempotent.

    SQL-only. No LLM calls. Runs hourly via launchd.
    """

    pass_name = "decay"

    def run(self, conn: Any, project_id: Any, *, dry_run: bool = False) -> PassResult:
        """Cap strength for idle rows.

        If project_id is given, only rows in that project are touched.
        If project_id is None, all projects are swept (cross-project; safe
        because decay is a mathematical operation with no data leakage).
        """
        if dry_run:
            count = _count_decay_candidates(conn, project_id)
            return PassResult(
                rows_processed=0,
                llm_calls=0,
                metadata={"pass": "decay", "dry_run_would_process": count},
            )

        rows_decayed = _apply_decay(conn, project_id)
        return PassResult(
            rows_processed=rows_decayed,
            llm_calls=0,
            metadata={"pass": "decay"},
        )


def _tier_where(days: int, project_id: Any) -> tuple[str, dict]:
    """WHERE clause for active rows idle >= ``days`` and above the ceiling."""
    where = (
        "archived_at IS NULL "
        "AND strength > %(cap)s "
        "AND " + IDLE_EXPR + " < NOW() - make_interval(days => %(days)s)"
    )
    params: dict[str, Any] = {"days": days}
    if project_id is not None:
        where += " AND project_id = %(project_id)s"
        params["project_id"] = project_id
    return where, params


def _count_decay_candidates(conn: Any, project_id: Any) -> int:
    """Rows the next run would change (each counted once, at its deepest tier)."""
    total = 0
    seen_days = None
    with conn.cursor() as cur:
        for days, cap in DECAY_TIERS:
            where, params = _tier_where(days, project_id)
            if seen_days is not None:
                where += " AND " + IDLE_EXPR + " >= NOW() - make_interval(days => %(upper)s)"
                params["upper"] = seen_days
            params["cap"] = cap
            cur.execute("SELECT COUNT(*) FROM devbrain.memory WHERE " + where, params)
            total += cur.fetchone()[0]
            seen_days = days
    return total


def _apply_decay(conn: Any, project_id: Any) -> int:
    """Apply each tier's ceiling, deepest tier first. Returns rows changed.

    Running the 90-day tier first means a 100-day-idle row ends at 0.12;
    the 30-day UPDATE then leaves it alone because 0.12 < 0.50.
    """
    total = 0
    with conn.cursor() as cur:
        for days, cap in DECAY_TIERS:
            where, params = _tier_where(days, project_id)
            params["cap"] = cap
            cur.execute(
                "UPDATE devbrain.memory SET strength = %(cap)s WHERE " + where,
                params,
            )
            total += cur.rowcount
    conn.commit()
    logger.info("cognify_decay: capped %d rows (project=%s)", total, project_id)
    return total
