"""Three-signal feedback loop for lesson graduation + rule demotion.

Public API:
- apply_feedback_signals(conn, job_id, brief, eval_results)
- demote_low_precision_rules(conn, project_id)
"""
from __future__ import annotations

# Tunable constants — change here if real-world data shows them wrong.
GRADUATION_STREAK_THRESHOLD = 3
GRADUATION_FRESHNESS_WINDOW = "90 days"
DEMOTION_PRECISION_THRESHOLD = 0.50
DEMOTION_WINDOW = "30 days"


def apply_feedback_signals(conn, job_id, brief, eval_results):
    """Stub. Implementation lands in 6c."""
    raise NotImplementedError("ships in Phase 6c")


def demote_low_precision_rules(conn, project_id):
    """Stub. Implementation lands in 6c."""
    raise NotImplementedError("ships in Phase 6c")
