"""Test migration 022 applies cleanly and creates the expected schema objects.

Migration 022 adds:
  - devbrain.memory.compliance_profiles TEXT[] — per-rule profile tags
    (NULL/[] = explicit opt-out from every project's brief)
  - devbrain.projects.compliance_profiles_enabled TEXT[] — projects opt
    into specific profiles; curator brief intersects the two arrays
  - idx_memory_compliance_profiles_gin partial GIN index — predicate
    `WHERE compliance_profiles IS NOT NULL` keeps the index lean since
    the vast majority of memory rows will have NULL profiles.

Verification gates for Phase 7a (substrate) live in P6/P7; this file
only asserts the schema shape so a regression that drops a column,
flips its type, or alters the index definition fails CI loudly.
"""
from __future__ import annotations

import pytest


@pytest.mark.db
def test_022_memory_has_compliance_profiles_column(conn):
    """devbrain.memory.compliance_profiles exists and is text[] (array of text)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT data_type, udt_name FROM information_schema.columns "
            "WHERE table_schema='devbrain' AND table_name='memory' "
            "  AND column_name='compliance_profiles'"
        )
        row = cur.fetchone()
    assert row is not None, "devbrain.memory.compliance_profiles is missing"
    data_type, udt_name = row
    # Postgres reports text[] as data_type='ARRAY' + udt_name='_text'.
    assert data_type == "ARRAY"
    assert udt_name == "_text"


@pytest.mark.db
def test_022_projects_has_compliance_profiles_enabled_column(conn):
    """devbrain.projects.compliance_profiles_enabled exists and is text[]."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT data_type, udt_name FROM information_schema.columns "
            "WHERE table_schema='devbrain' AND table_name='projects' "
            "  AND column_name='compliance_profiles_enabled'"
        )
        row = cur.fetchone()
    assert row is not None, (
        "devbrain.projects.compliance_profiles_enabled is missing"
    )
    data_type, udt_name = row
    assert data_type == "ARRAY"
    assert udt_name == "_text"


@pytest.mark.db
def test_022_compliance_profiles_gin_index_exists(conn):
    """idx_memory_compliance_profiles_gin must exist on devbrain.memory."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname='devbrain' "
            "  AND tablename='memory' "
            "  AND indexname='idx_memory_compliance_profiles_gin'"
        )
        row = cur.fetchone()
    assert row is not None, "idx_memory_compliance_profiles_gin is missing"


@pytest.mark.db
def test_022_compliance_profiles_gin_index_uses_partial_predicate(conn):
    """The GIN index must be partial (`WHERE compliance_profiles IS NOT NULL`)
    and use the GIN access method. Both checks read from the same indexdef
    string Postgres gives us, so they're co-located here."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname='devbrain' "
            "  AND tablename='memory' "
            "  AND indexname='idx_memory_compliance_profiles_gin'"
        )
        row = cur.fetchone()
    assert row is not None
    indexdef = row[0]
    assert "USING gin" in indexdef
    assert "compliance_profiles IS NOT NULL" in indexdef
