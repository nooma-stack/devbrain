"""Three-signal feedback loop for lesson graduation + rule demotion.

MOVED TO factory/cognify/strengthen.py (Atlas Phase 6d).

This module is now a thin backwards-compatibility shim. All logic lives in
``cognify.strengthen``. Existing callers (state_machine.py, tests) continue
to import from ``curator.graduation`` unchanged.

Public API (re-exported from cognify.strengthen — behaviour unchanged):
- apply_feedback_signals(conn, job_id, brief, eval_results)
- demote_low_precision_rules(conn, project_id)
- GRADUATION_STREAK_THRESHOLD
- GRADUATION_FRESHNESS_WINDOW
- DEMOTION_PRECISION_THRESHOLD
- DEMOTION_WINDOW
- _collect_brief_memory_ids
- _index_findings_by_memory
- _signal_failure
- _signal_success
- _graduate
"""
from __future__ import annotations

from cognify.strengthen import (  # noqa: F401
    DEMOTION_PRECISION_THRESHOLD,
    DEMOTION_WINDOW,
    GRADUATION_FRESHNESS_WINDOW,
    GRADUATION_STREAK_THRESHOLD,
    _collect_brief_memory_ids,
    _graduate,
    _index_findings_by_memory,
    _signal_failure,
    _signal_success,
    apply_feedback_signals,
    demote_low_precision_rules,
)
