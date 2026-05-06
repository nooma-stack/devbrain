"""spend.py — write rows to devbrain.cognify_spend_log.

A thin wrapper around the INSERT so callers (extract, edges, eval runner)
don't embed raw SQL. All columns map 1-to-1 with the migration 029 schema.

PHI constraint: this module NEVER receives or logs raw session content —
only token counts, model name, pass name, project_id, and timestamps.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


def record_spend(
    conn: Any,
    *,
    project_id: UUID | str | None,
    pass_name: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    cost_usd: float,
) -> int | None:
    """Insert one row into devbrain.cognify_spend_log.

    Args:
        conn: psycopg2 connection. The caller owns the transaction; this
            function commits the INSERT independently so a downstream
            failure doesn't roll back the spend record.
        project_id: UUID of the project. May be None for cross-project passes
            (but spend_log rows are most useful when project_id is set).
        pass_name: Name of the cognify pass or eval agent (e.g. 'extract',
            'edges', 'eval_security').
        model: Anthropic model identifier string (e.g. 'claude-sonnet-4-6').
        input_tokens: Non-cached input tokens from the response usage block.
        output_tokens: Output tokens from the response usage block.
        cache_read_tokens: Prompt-cache-read tokens (0 if not applicable).
        cache_write_tokens: Prompt-cache-write tokens (0 if not applicable).
        cost_usd: Estimated cost in USD (from observability.pricing.compute_cost_usd).

    Returns:
        The inserted row id (BIGINT) or None if the INSERT failed.
        Failures are logged at WARNING but not re-raised — spend logging
        must never abort a cognify pass.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO devbrain.cognify_spend_log "
                "(project_id, pass_name, model, "
                " input_tokens, output_tokens, "
                " cache_read_tokens, cache_write_tokens, "
                " cost_usd) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id",
                (
                    project_id,
                    pass_name,
                    model,
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_write_tokens,
                    cost_usd,
                ),
            )
            row_id = cur.fetchone()[0]
        conn.commit()
        return row_id
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "record_spend: failed to write spend log row (pass=%s, model=%s): %s",
            pass_name,
            model,
            exc,
        )
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
