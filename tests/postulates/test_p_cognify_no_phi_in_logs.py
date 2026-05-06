"""P_cognify_no_phi_in_logs: cognify_run_log.metadata never contains raw
memory.content (potential PHI).

This postulate verifies the design contract: cognify passes are only allowed
to log row counts, IDs, and metadata about the pass — not the actual text
content of memory rows.

We seed a memory row whose content looks like PHI, run a pass that would
log metadata about it, and then inspect the cognify_run_log row to confirm
the content string doesn't appear.
"""
from __future__ import annotations

import json
import uuid

import pytest

from cognify.decay import DecayPass
from cognify.gc import GCPass


PHI_SENTINEL = "PATIENT_NAME=John_Doe_SSN_123-45-6789"


def _setup_row(conn, project_id):
    """Insert a memory row whose content looks like PHI."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory "
            "(project_id, kind, title, content) "
            "VALUES (%s, 'decision', 'PHI test row', %s) "
            "RETURNING id",
            (project_id, PHI_SENTINEL),
        )
        mid = cur.fetchone()[0]
    conn.commit()
    return mid


def _set_idle_and_low_strength(conn, memory_id, days=100):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory "
            "SET strength = 0.05, "
            "    last_cascade_at = NOW() - INTERVAL '" + str(days) + " days', "
            "    last_hit = NOW() - INTERVAL '" + str(days) + " days', "
            "    created_at = NOW() - INTERVAL '" + str(days) + " days' "
            "WHERE id = %s",
            (memory_id,),
        )
    conn.commit()


def _latest_run_log_metadata(conn, pass_name, project_id) -> str:
    """Fetch the most recent cognify_run_log metadata as a string."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT metadata FROM devbrain.cognify_run_log "
            "WHERE pass_name = %s AND project_id = %s "
            "ORDER BY started_at DESC LIMIT 1",
            (pass_name, project_id),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return ""
    return json.dumps(row[0])


@pytest.mark.db
def test_p_cognify_no_phi_in_decay_log(conn, project_factory):
    """cognify_decay run log metadata must not contain PHI from memory.content."""
    project = project_factory("p_nophi_decay")
    mid = _setup_row(conn, project["id"])
    _set_idle_and_low_strength(conn, mid, days=45)

    from cognify.orchestrator import run_pass
    run_pass(conn, "decay", project["id"])

    meta_str = _latest_run_log_metadata(conn, "decay", project["id"])
    assert PHI_SENTINEL not in meta_str, (
        "cognify_run_log metadata contains raw PHI from memory.content"
    )


@pytest.mark.db
def test_p_cognify_no_phi_in_gc_log(conn, project_factory):
    """cognify_gc run log metadata must not contain PHI from memory.content."""
    project = project_factory("p_nophi_gc")
    mid = _setup_row(conn, project["id"])
    _set_idle_and_low_strength(conn, mid, days=100)

    from cognify.orchestrator import run_pass
    run_pass(conn, "gc", project["id"])

    meta_str = _latest_run_log_metadata(conn, "gc", project["id"])
    assert PHI_SENTINEL not in meta_str, (
        "cognify_run_log metadata contains raw PHI from memory.content"
    )
