"""Test migration 021 applies cleanly and creates the expected schema objects.

Migration 021 adds:
  - devbrain.refinement_queue table — signal #2 cases (memories that
    should have been in the brief but weren't) queued for end-of-tick
    applies_when widening
  - idx_refinement_queue_pending partial index — `WHERE applied_at IS
    NULL`, optimizes the per-tick dequeue scan

Note: this was originally migration 020 in the plan, but 020 was used in
Phase 6c for the effective_hit_count column. See migrations/021 header
for context.
"""
from __future__ import annotations

import pytest


EXPECTED_COLUMNS = {
    "id",
    "memory_id",
    "file_pattern",
    "keywords",
    "queued_at",
    "applied_at",
    "error",
}


@pytest.mark.db
def test_021_refinement_queue_table_exists(conn):
    """devbrain.refinement_queue table must exist."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='devbrain' AND table_name='refinement_queue'"
        )
        row = cur.fetchone()
    assert row is not None, "devbrain.refinement_queue table is missing"


@pytest.mark.db
def test_021_refinement_queue_has_all_columns(conn):
    """All 7 expected columns must be present on refinement_queue."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='devbrain' AND table_name='refinement_queue'"
        )
        cols = {r[0] for r in cur.fetchall()}
    missing = EXPECTED_COLUMNS - cols
    extra = cols - EXPECTED_COLUMNS
    assert not missing, f"missing columns on refinement_queue: {missing}"
    assert not extra, f"unexpected columns on refinement_queue: {extra}"


@pytest.mark.db
def test_021_refinement_queue_pending_index_exists(conn):
    """idx_refinement_queue_pending must be a partial index on queued_at
    with the predicate `applied_at IS NULL` so the dequeue scan only walks
    pending rows."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname='devbrain' "
            "  AND tablename='refinement_queue' "
            "  AND indexname='idx_refinement_queue_pending'"
        )
        row = cur.fetchone()
    assert row is not None, "idx_refinement_queue_pending is missing"
    indexdef = row[0]
    assert "queued_at" in indexdef
    assert "applied_at IS NULL" in indexdef
