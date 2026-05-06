"""factory.cognify — scheduled knowledge-extraction passes.

This package implements the five cognify passes that run independently of
session ingest events (memify). Each pass has its own cadence and cost profile:

    cognify_decay     — hourly,   0 LLM calls, time-based strength decay
    cognify_extract   — hourly,   up to 20 LLM calls, lesson/decision extraction
    cognify_edges     — 6-hourly, up to 15 LLM calls, cites+contradicts inference
    cognify_strengthen — daily,   0 LLM calls, lesson graduation (from graduation.py)
    cognify_gc        — weekly,   0 LLM calls, archive low-strength orphans

All passes are project-scoped. Runs are logged in devbrain.cognify_run_log
(migration 025) for idempotency and observability.

See docs/plans/2026-05-05-phase-6-cognify-memify-design.md for the full design.
"""
