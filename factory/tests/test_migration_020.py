"""Test migration 020 applies cleanly and creates the expected schema objects.

Migration 020 adds:
  - devbrain.memory.effective_hit_count (INTEGER NOT NULL DEFAULT 0) —
    counts in-brief, no-violation events (signal #3). Used together with
    hit_count to compute precision = effective_hit_count /
    (hit_count + effective_hit_count) for the demote sweep.

This column was missed by migration 019 but is required by the graduation
pipeline shipped in Step 6c (factory/curator/graduation.py).
"""
from __future__ import annotations

import pytest


@pytest.mark.db
def test_020_memory_has_effective_hit_count(conn):
    """effective_hit_count must exist as INTEGER NOT NULL DEFAULT 0."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT data_type, is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_schema='devbrain' AND table_name='memory' "
            "AND column_name='effective_hit_count'"
        )
        row = cur.fetchone()
    assert row is not None, "effective_hit_count column is missing"
    data_type, is_nullable, column_default = row
    assert data_type == "integer"
    assert is_nullable == "NO"
    assert column_default == "0"
