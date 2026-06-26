"""The per-(pass, project) advisory lock keeps a manual cognify run from
racing the scheduled one and double-spending LLM calls."""

from __future__ import annotations

import psycopg2
import pytest

from cognify.orchestrator import (
    _release_pass_lock,
    _try_pass_lock,
    run_pass,
)


def _run_log_count(conn, pass_name, project_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM devbrain.cognify_run_log "
            "WHERE pass_name = %s AND project_id = %s",
            (pass_name, project_id),
        )
        return cur.fetchone()[0]


@pytest.mark.db
def test_pass_advisory_lock_blocks_concurrent_run(database_url, project_factory):
    project = project_factory("cognify_lock")
    pid = project["id"]
    conn_a = psycopg2.connect(database_url)
    conn_b = psycopg2.connect(database_url)
    try:
        # A acquires; B cannot.
        assert _try_pass_lock(conn_a, "decay", pid) is True
        assert _try_pass_lock(conn_b, "decay", pid) is False

        # run_pass on B must skip without writing a run-log row (so the
        # watermark/observability isn't touched by a no-op skip).
        before = _run_log_count(conn_b, "decay", pid)
        result = run_pass(conn_b, "decay", pid)
        assert result.metadata.get("skipped") == "lock_held"
        assert _run_log_count(conn_b, "decay", pid) == before

        # After A releases, B can acquire.
        _release_pass_lock(conn_a, "decay", pid)
        assert _try_pass_lock(conn_b, "decay", pid) is True
        _release_pass_lock(conn_b, "decay", pid)
    finally:
        conn_a.close()
        conn_b.close()


@pytest.mark.db
def test_dry_run_does_not_contend_on_lock(database_url, project_factory):
    """Dry runs are read-only and must not take or be blocked by the lock."""
    project = project_factory("cognify_lock_dry")
    pid = project["id"]
    conn_a = psycopg2.connect(database_url)
    conn_b = psycopg2.connect(database_url)
    try:
        assert _try_pass_lock(conn_a, "decay", pid) is True
        # Even with the lock held, a dry run proceeds (it skips the lock).
        result = run_pass(conn_b, "decay", pid, dry_run=True)
        assert result.metadata.get("skipped") != "lock_held"
        _release_pass_lock(conn_a, "decay", pid)
    finally:
        conn_a.close()
        conn_b.close()
