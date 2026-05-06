"""cognify_decay — time-based exponential strength decay.

Applies decay to memory rows whose strength has not been reinforced recently.
SQL-only pass: zero LLM cost, runs hourly.

Decay schedule (applied to rows not hit for the given interval):
  >= 30 days idle: strength *= 0.50
  >= 90 days idle: strength *= 0.12  (approximately e^(-2.12))

"Idle" is measured by max(last_cascade_at, hit_count_updated_at, last_hit).
Rows with strength already at 0.0 or archived rows are skipped.

Strength values are clamped to [0.001, 1.0] after decay so rows never
hit exactly 0 (they're handled by cognify_gc instead).

All writes go through the normal memory UPDATE path, which fires the AFTER
trigger from migration 015 (memory_ledger write). So every decay is audited.
"""
from __future__ import annotations

import logging
from typing import Any

from cognify.orchestrator import CognifyPass, PassResult, register_pass

logger = logging.getLogger(__name__)

# Decay multipliers per idle tier.
DECAY_30D_MULTIPLIER = 0.50   # >= 30 days idle: 50% strength retained
DECAY_90D_MULTIPLIER = 0.12   # >= 90 days idle: 12% strength retained

# Minimum post-decay strength (prevents exact zero; GC handles the bottom).
MIN_STRENGTH = 0.001


@register_pass
class DecayPass(CognifyPass):
    """cognify_decay: apply time-based exponential strength decay.

    SQL-only. No LLM calls. Runs hourly via launchd.
    """

    pass_name = "decay"

    def run(self, conn: Any, project_id: Any, *, dry_run: bool = False) -> PassResult:
        """Decay strength for idle memory rows.

        If project_id is given, only rows in that project are decayed.
        If project_id is None, all projects are swept (cross-project; safe
        because decay is a mathematical operation with no data leakage).
        """
        if dry_run:
            return self._dry_run(conn, project_id)

        rows_decayed = _apply_decay(conn, project_id)
        return PassResult(
            rows_processed=rows_decayed,
            llm_calls=0,
            metadata={"pass": "decay"},
        )

    def _dry_run(self, conn: Any, project_id: Any) -> PassResult:
        """Return a count of rows that would be decayed, without mutating."""
        sql, params = _build_decay_count_sql(project_id)
        with conn.cursor() as cur:
            cur.execute(sql, params)
            count = cur.fetchone()[0]
        return PassResult(
            rows_processed=0,
            llm_calls=0,
            metadata={"pass": "decay", "dry_run_would_process": count},
        )


def _idle_expr() -> str:
    """SQL expression for the most-recent activity timestamp of a memory row."""
    return (
        "GREATEST("
        "  last_cascade_at, "
        "  last_hit, "
        "  created_at"        # fallback: creation date for never-hit rows
        ")"
    )


def _build_decay_count_sql(project_id: Any) -> tuple[str, dict]:
    """Return (sql, params) for counting rows that would be decayed."""
    idle_expr = _idle_expr()
    min_s = str(MIN_STRENGTH)
    if project_id is not None:
        sql = (
            "SELECT COUNT(*) FROM devbrain.memory "
            "WHERE archived_at IS NULL "
            "  AND strength > " + min_s + " "
            "  AND project_id = %(project_id)s "
            "  AND ("
            "      " + idle_expr + " < NOW() - INTERVAL '30 days' "
            "      OR " + idle_expr + " IS NULL"
            "  )"
        )
        params = {"project_id": project_id}
    else:
        sql = (
            "SELECT COUNT(*) FROM devbrain.memory "
            "WHERE archived_at IS NULL "
            "  AND strength > " + min_s + " "
            "  AND ("
            "      " + idle_expr + " < NOW() - INTERVAL '30 days' "
            "      OR " + idle_expr + " IS NULL"
            "  )"
        )
        params = {}
    return sql, params


def _apply_decay(conn: Any, project_id: Any) -> int:
    """Apply decay in two passes (90d tier first, then 30d tier).

    Two-pass avoids double-applying: a row that qualifies for the 90d tier
    gets the 90d multiplier (not 90d * 30d). The 90d UPDATE runs first;
    the 30d UPDATE then only matches rows NOT yet in the 90d tier (i.e.
    30d <= idle < 90d).

    NOTE: SQL is built with string concatenation for non-user-controlled
    constants (MIN_STRENGTH, idle_expr, intervals) and psycopg2 %s
    parameters only for user-supplied values (project_id, multipliers).
    We avoid mixing f-string %s with psycopg2 %s to prevent
    "not all arguments converted" errors.
    """
    idle_expr = _idle_expr()
    min_s = str(MIN_STRENGTH)

    total = 0

    # ── 90-day tier ───────────────────────────────────────────────────────────
    if project_id is not None:
        sql_90 = (
            "UPDATE devbrain.memory "
            "SET strength = GREATEST(" + str(DECAY_90D_MULTIPLIER) + "::float * strength, " + min_s + ") "
            "WHERE archived_at IS NULL "
            "  AND strength > " + min_s + " "
            "  AND project_id = %(project_id)s "
            "  AND ("
            "      " + idle_expr + " < NOW() - INTERVAL '90 days' "
            "      OR (" + idle_expr + " IS NULL AND created_at < NOW() - INTERVAL '90 days')"
            "  )"
        )
        params = {"project_id": project_id}
    else:
        sql_90 = (
            "UPDATE devbrain.memory "
            "SET strength = GREATEST(" + str(DECAY_90D_MULTIPLIER) + "::float * strength, " + min_s + ") "
            "WHERE archived_at IS NULL "
            "  AND strength > " + min_s + " "
            "  AND ("
            "      " + idle_expr + " < NOW() - INTERVAL '90 days' "
            "      OR (" + idle_expr + " IS NULL AND created_at < NOW() - INTERVAL '90 days')"
            "  )"
        )
        params = {}

    with conn.cursor() as cur:
        cur.execute(sql_90, params)
        total += cur.rowcount
    conn.commit()

    # ── 30-day tier ───────────────────────────────────────────────────────────
    if project_id is not None:
        sql_30 = (
            "UPDATE devbrain.memory "
            "SET strength = GREATEST(" + str(DECAY_30D_MULTIPLIER) + "::float * strength, " + min_s + ") "
            "WHERE archived_at IS NULL "
            "  AND strength > " + min_s + " "
            "  AND project_id = %(project_id)s "
            "  AND ("
            "      " + idle_expr + " >= NOW() - INTERVAL '90 days' "
            "      OR (" + idle_expr + " IS NULL AND created_at >= NOW() - INTERVAL '90 days')"
            "  ) "
            "  AND ("
            "      " + idle_expr + " < NOW() - INTERVAL '30 days' "
            "      OR (" + idle_expr + " IS NULL AND created_at < NOW() - INTERVAL '30 days')"
            "  )"
        )
    else:
        sql_30 = (
            "UPDATE devbrain.memory "
            "SET strength = GREATEST(" + str(DECAY_30D_MULTIPLIER) + "::float * strength, " + min_s + ") "
            "WHERE archived_at IS NULL "
            "  AND strength > " + min_s + " "
            "  AND ("
            "      " + idle_expr + " >= NOW() - INTERVAL '90 days' "
            "      OR (" + idle_expr + " IS NULL AND created_at >= NOW() - INTERVAL '90 days')"
            "  ) "
            "  AND ("
            "      " + idle_expr + " < NOW() - INTERVAL '30 days' "
            "      OR (" + idle_expr + " IS NULL AND created_at < NOW() - INTERVAL '30 days')"
            "  )"
        )

    with conn.cursor() as cur:
        cur.execute(sql_30, params)
        total += cur.rowcount
    conn.commit()

    logger.info("cognify_decay: decayed %d rows (project=%s)", total, project_id)
    return total
