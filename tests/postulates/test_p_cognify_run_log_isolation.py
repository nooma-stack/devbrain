"""P_cognify_run_log_isolation: cognify_run_log rows are project-scoped;
cross-project queries don't leak.

This postulate verifies that:
  1. Each run log row is tagged with a project_id.
  2. Querying run log for project A cannot see rows from project B.
"""
from __future__ import annotations

import pytest

from cognify.orchestrator import run_pass


@pytest.mark.db
def test_p_cognify_run_log_isolation(conn, project_factory):
    """Run log for project A must not be visible when querying project B."""
    project_a = project_factory("p_rlog_isola")
    project_b = project_factory("p_rlog_isolb")

    # Run decay for project A — creates a run_log row with project_a's id.
    run_pass(conn, "decay", project_a["id"])

    # Query run log rows for project B — must return 0 rows from this pass.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.cognify_run_log "
            "WHERE pass_name = 'decay' AND project_id = %s",
            (project_b["id"],),
        )
        count_b = cur.fetchone()[0]

    # Also verify project A has at least 1 row
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.cognify_run_log "
            "WHERE pass_name = 'decay' AND project_id = %s",
            (project_a["id"],),
        )
        count_a = cur.fetchone()[0]

    assert count_a >= 1, "No run log row created for project A"
    assert count_b == 0, (
        f"Run log for project B has {count_b} rows from project A's pass run "
        "(cross-project leak)"
    )
