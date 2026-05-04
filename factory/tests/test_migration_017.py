"""Test migration 017 applies cleanly and creates the expected schema objects.

Migration 017 adds:
  - devbrain.curator_re_eval_queue table (drained by cascade worker)
  - devbrain.memory.last_cascade_at column (audit timestamp)
  - devbrain.factory_jobs.curator_brief column (cached JSONB brief)
  - idx_re_eval_queue_dedup partial unique index (post-review refinement;
    prevents double-penalty on concurrent enqueues for the same triplet)

These tests assert the schema is in place; behavioral correctness of the
queue (FIFO drainage, dedup semantics, attempt_count gating) is covered
by the cascade-worker tests in Task 5c.
"""
from __future__ import annotations

import pytest


@pytest.mark.db
def test_017_creates_curator_queue_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='devbrain' AND table_name='curator_re_eval_queue'"
        )
        assert cur.fetchone() is not None


@pytest.mark.db
def test_017_curator_queue_has_required_columns(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='devbrain' AND table_name='curator_re_eval_queue'"
        )
        cols = {row[0] for row in cur.fetchall()}
    assert {
        "id",
        "memory_id",
        "cascade_source_id",
        "edge_type",
        "enqueued_at",
        "attempt_count",
        "last_error",
    } <= cols


@pytest.mark.db
def test_017_curator_queue_has_dedup_unique_index(conn):
    """The dedup partial unique index (post-review refinement) prevents the
    cascade penalty (additive, not idempotent) from double-firing when two
    enqueues race for the same (memory_id, cascade_source_id, edge_type).

    Failed rows (attempt_count = 3) are deliberately excluded from the index
    so legitimate re-enqueues aren't blocked after triage.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname='devbrain' "
            "  AND tablename='curator_re_eval_queue' "
            "  AND indexname='idx_re_eval_queue_dedup'"
        )
        row = cur.fetchone()
    assert row is not None, "idx_re_eval_queue_dedup is missing"
    indexdef = row[0]
    assert "UNIQUE" in indexdef
    assert "memory_id" in indexdef
    assert "cascade_source_id" in indexdef
    assert "edge_type" in indexdef
    assert "attempt_count < 3" in indexdef


@pytest.mark.db
def test_017_memory_has_last_cascade_at(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='devbrain' AND table_name='memory' "
            "AND column_name='last_cascade_at'"
        )
        assert cur.fetchone() is not None


@pytest.mark.db
def test_017_factory_jobs_has_curator_brief(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='devbrain' AND table_name='factory_jobs' "
            "AND column_name='curator_brief'"
        )
        assert cur.fetchone() is not None
