"""Test migration 019 applies cleanly and creates the expected schema objects.

Migration 019 adds:
  - devbrain.memory.current_streak (INTEGER NOT NULL DEFAULT 0) — consecutive
    successful preventions; signal #3 increments, signal #1 resets
  - devbrain.memory.graduated_at (TIMESTAMPTZ) — set on tier transition
    'lesson' -> 'rule'
  - devbrain.memory.demoted_at (TIMESTAMPTZ) — set on tier transition
    'rule' -> 'lesson'
  - idx_memory_graduation_candidates partial index — optimizes the
    graduation candidate query at end of every REVIEWING phase

These tests assert the schema is in place; behavioral correctness of the
graduation pipeline is covered by the graduation-module tests in Task 6c.
"""
from __future__ import annotations

import pytest


@pytest.mark.db
def test_019_memory_has_current_streak(conn):
    """current_streak must exist as INTEGER NOT NULL DEFAULT 0."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema='devbrain' AND table_name='memory' "
            "AND column_name='current_streak'"
        )
        row = cur.fetchone()
    assert row is not None, "current_streak column is missing"
    data_type, is_nullable, column_default = row
    assert data_type == "integer"
    assert is_nullable == "NO"
    assert column_default == "0"


@pytest.mark.db
def test_019_memory_has_graduated_at(conn):
    """graduated_at must exist as TIMESTAMPTZ, nullable."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema='devbrain' AND table_name='memory' "
            "AND column_name='graduated_at'"
        )
        row = cur.fetchone()
    assert row is not None, "graduated_at column is missing"
    data_type, is_nullable = row
    assert data_type == "timestamp with time zone"
    assert is_nullable == "YES"


@pytest.mark.db
def test_019_memory_has_demoted_at(conn):
    """demoted_at must exist as TIMESTAMPTZ, nullable."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema='devbrain' AND table_name='memory' "
            "AND column_name='demoted_at'"
        )
        row = cur.fetchone()
    assert row is not None, "demoted_at column is missing"
    data_type, is_nullable = row
    assert data_type == "timestamp with time zone"
    assert is_nullable == "YES"


@pytest.mark.db
def test_019_graduation_candidates_index(conn):
    """idx_memory_graduation_candidates must be a partial index on
    last_hit DESC with the predicate filtering to lesson tier, streak >= 3,
    not archived. This is the hot-path query at end of every REVIEWING phase.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname='devbrain' "
            "  AND tablename='memory' "
            "  AND indexname='idx_memory_graduation_candidates'"
        )
        row = cur.fetchone()
    assert row is not None, "idx_memory_graduation_candidates is missing"
    indexdef = row[0]
    assert "last_hit DESC" in indexdef
    assert "tier = 'lesson'" in indexdef
    assert "current_streak >= 3" in indexdef
    assert "archived_at IS NULL" in indexdef
