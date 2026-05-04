"""Test migration 018 applies cleanly and creates the expected schema objects.

Migration 018 adds:
  - devbrain.end_session_log table (idempotency log for the MCP end_session
    tool; PRIMARY KEY (session_id, payload_hash))
  - idx_end_session_log_project_recent index (project-scoped recency view)
  - schema_migrations entry

These tests assert the schema is in place; behavioral correctness of the
idempotent handler is covered by P_end_session_idempotent in
tests/postulates/ and by factory/tests/test_curator_end_session.py.
"""
from __future__ import annotations

import pytest


@pytest.mark.db
def test_018_creates_end_session_log_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='devbrain' AND table_name='end_session_log'"
        )
        assert cur.fetchone() is not None


@pytest.mark.db
def test_018_end_session_log_has_required_columns(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='devbrain' AND table_name='end_session_log'"
        )
        cols = {row[0] for row in cur.fetchall()}
    assert {
        "session_id",
        "payload_hash",
        "project_id",
        "applied_at",
        "result",
    } <= cols


@pytest.mark.db
def test_018_end_session_log_pk_is_session_plus_hash(conn):
    """PK = (session_id, payload_hash). Same session_id + different payload =
    new row (corrected judgment). Same session_id + same payload = duplicate
    rejected, returns the original result.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.attname "
            "FROM pg_index i "
            "JOIN pg_attribute a ON a.attrelid = i.indrelid "
            "                   AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'devbrain.end_session_log'::regclass "
            "  AND i.indisprimary "
            "ORDER BY array_position(i.indkey, a.attnum)"
        )
        cols = [row[0] for row in cur.fetchall()]
    assert cols == ["session_id", "payload_hash"]


@pytest.mark.db
def test_018_recorded_in_schema_migrations(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM devbrain.schema_migrations "
            "WHERE filename = '018_end_session_log.sql'"
        )
        assert cur.fetchone() is not None
