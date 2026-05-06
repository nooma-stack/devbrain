"""factory.observability — spend tracking and cost observability helpers.

Provides per-call LLM cost capture written to devbrain.cognify_spend_log
(migration 029). No raw PHI is stored — only token counts, model name,
pass name, project_id, and timestamps.

Usage::

    from observability.pricing import SONNET_4_6, compute_cost_usd
    from observability.spend import record_spend

    cost = compute_cost_usd(
        SONNET_4_6,
        input_tokens=...,
        output_tokens=...,
        cache_read_tokens=...,
        cache_write_tokens=...,
    )
    record_spend(conn, project_id=..., pass_name=..., model=..., ..., cost_usd=cost)
"""
