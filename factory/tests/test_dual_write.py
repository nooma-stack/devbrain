"""Tests for P2.b adapter dual-write into devbrain.memory.

Covers all five `kind` values (chunk/decision/pattern/issue/
session_summary), the partial unique-index idempotency from migration
011, and the load-bearing contract that a failed memory dual-write
must not roll back the surrounding legacy commit (psycopg2 savepoint
discipline in ingest/memory_writer.py).

The Python helper `record_memory` is shared by all call sites and is
the unit under direct test for kinds 2-5; the chunk-kind test goes
through `ingest.db.insert_chunk` to exercise the actual call site.
The mcp-server TypeScript adapter calls a structurally identical
`recordMemory` helper — the partial unique index, embedding reuse,
and best-effort semantics validated here are the same contract those
paths rely on.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

# Mirror the production sys.path layout: factory/ for config, ingest/
# for memory_writer + insert_chunk.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent.parent / "ingest")
)

from config import DATABASE_URL  # noqa: E402  (factory/config.py)
from db import insert_chunk  # noqa: E402  (ingest/db.py)
from memory_writer import record_memory  # noqa: E402  (ingest/memory_writer.py)
from state_machine import FactoryDB  # noqa: E402

# All test rows have content starting with this prefix so the autouse
# cleanup fixture can wipe them with one LIKE query (works even for
# chunk-kind memory rows whose title is NULL).
TEST_CONTENT_PREFIX = "dual_write_test_"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture(autouse=True)
def _cleanup(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM devbrain.memory WHERE content LIKE %s",
            (f"{TEST_CONTENT_PREFIX}%",),
        )
        cur.execute(
            "DELETE FROM devbrain.chunks WHERE content LIKE %s",
            (f"{TEST_CONTENT_PREFIX}%",),
        )
        # legacy decisions: title and context are both prefixed in
        # test_legacy_survives_memory_failure
        cur.execute(
            "DELETE FROM devbrain.decisions "
            "WHERE title LIKE %s OR context LIKE %s",
            (f"{TEST_CONTENT_PREFIX}%", f"{TEST_CONTENT_PREFIX}%"),
        )
        conn.commit()


def _devbrain_project_id(db) -> str:
    """The seeded 'devbrain' project (migration 001) — used as a real
    FK target instead of creating a throwaway project per test."""
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.projects WHERE slug = 'devbrain'"
        )
        return cur.fetchone()[0]


def _embedding_sql(value: float = 0.0) -> str:
    return "[" + ",".join([str(value)] * 1024) + "]"


# ─── 1. Migration 011 applied + index present ────────────────────────────


def test_migration_011_applied(db):
    """If 011 hasn't run, the inferred-constraint ON CONFLICT clause
    in record_memory will error (Postgres can't match a partial unique
    index without one). Surface that as a clear failure here."""
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM devbrain.schema_migrations "
            "WHERE filename = %s",
            ("011_memory_provenance_unique.sql",),
        )
        assert cur.fetchone() is not None, (
            "011_memory_provenance_unique.sql is not recorded in "
            "schema_migrations — run `bin/devbrain migrate`"
        )
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = 'devbrain' AND tablename = 'memory' "
            "AND indexname = 'idx_memory_provenance_kind_unique'"
        )
        row = cur.fetchone()
        assert row is not None, "partial unique index missing"
        idxdef = row[0]
        # Index must be UNIQUE on (provenance_id, kind) WITH the
        # provenance_id IS NOT NULL predicate — both halves are
        # required for the inferred-constraint match.
        assert "UNIQUE" in idxdef.upper()
        assert "provenance_id" in idxdef
        assert "kind" in idxdef
        assert "provenance_id IS NOT NULL" in idxdef


# ─── 2-6. Dual-write produces a memory row for each kind ─────────────────


def test_dual_write_decision(db):
    """kind='decision' dual-write: one memory row, embedding+title+
    content+provenance match, tier defaults to 'memory'."""
    pid = _devbrain_project_id(db)
    prov = "11111111-1111-1111-1111-111111111111"
    content = f"{TEST_CONTENT_PREFIX}decision body"
    embedding_sql = _embedding_sql(0.1)

    with db._conn() as conn, conn.cursor() as cur:
        record_memory(
            cur,
            project_id=pid,
            kind="decision",
            content=content,
            title=f"{TEST_CONTENT_PREFIX}decision title",
            embedding_sql=embedding_sql,
            provenance_id=prov,
        )
        conn.commit()

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT project_id, kind, title, content, "
            "       provenance_id, tier, embedding IS NOT NULL "
            "FROM devbrain.memory WHERE content = %s",
            (content,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    project_id, kind, title, content_db, prov_db, tier, has_emb = rows[0]
    assert str(project_id) == str(pid)
    assert kind == "decision"
    assert title == f"{TEST_CONTENT_PREFIX}decision title"
    assert content_db == content
    assert str(prov_db) == prov
    assert tier == "memory"
    assert has_emb is True


def test_dual_write_pattern(db):
    """kind='pattern' dual-write — exercises the factory.learning
    call-site shape (provenance_id is the patterns row UUID)."""
    pid = _devbrain_project_id(db)
    prov = "22222222-2222-2222-2222-222222222222"
    content = f"{TEST_CONTENT_PREFIX}pattern body"

    with db._conn() as conn, conn.cursor() as cur:
        record_memory(
            cur,
            project_id=pid,
            kind="pattern",
            content=content,
            title=f"{TEST_CONTENT_PREFIX}pattern_name",
            embedding_sql=_embedding_sql(0.2),
            provenance_id=prov,
        )
        conn.commit()

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT kind, content, provenance_id "
            "FROM devbrain.memory WHERE content = %s",
            (content,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "pattern"
    assert rows[0][1] == content
    assert str(rows[0][2]) == prov


def test_dual_write_issue(db):
    """kind='issue' dual-write — exercises the MCP store call-site
    shape for issue records."""
    pid = _devbrain_project_id(db)
    prov = "33333333-3333-3333-3333-333333333333"
    content = f"{TEST_CONTENT_PREFIX}issue body"

    with db._conn() as conn, conn.cursor() as cur:
        record_memory(
            cur,
            project_id=pid,
            kind="issue",
            content=content,
            title=f"{TEST_CONTENT_PREFIX}issue title",
            embedding_sql=_embedding_sql(0.3),
            provenance_id=prov,
        )
        conn.commit()

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT kind, provenance_id FROM devbrain.memory "
            "WHERE content = %s",
            (content,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "issue"
    assert str(rows[0][1]) == prov


def test_dual_write_session_summary(db):
    """kind='session_summary' — the MCP end_session anchor uses the
    chunks row id as provenance (no raw_sessions write in that
    path); test the same shape."""
    pid = _devbrain_project_id(db)
    prov = "44444444-4444-4444-4444-444444444444"
    content = f"{TEST_CONTENT_PREFIX}session_summary body"

    with db._conn() as conn, conn.cursor() as cur:
        record_memory(
            cur,
            project_id=pid,
            kind="session_summary",
            content=content,
            embedding_sql=_embedding_sql(0.4),
            provenance_id=prov,
        )
        conn.commit()

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT kind, title, provenance_id FROM devbrain.memory "
            "WHERE content = %s",
            (content,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "session_summary"
    assert rows[0][1] is None  # session_summary has no separate title
    assert str(rows[0][2]) == prov


def test_dual_write_chunk_via_insert_chunk(db):
    """kind='chunk' goes through ingest.db.insert_chunk — the actual
    call site (not record_memory directly). Verifies the dual-write
    is wired in there AND that the project_id-None guard skips the
    memory write but keeps the legacy chunk insert."""
    pid = _devbrain_project_id(db)
    content = f"{TEST_CONTENT_PREFIX}chunk via insert_chunk"
    embedding = [0.5] * 1024

    chunk_id = insert_chunk(
        project_id=pid,
        source_type="session",
        source_id=None,
        source_line_start=None,
        source_line_end=None,
        content=content,
        embedding=embedding,
        token_count=10,
    )
    assert chunk_id  # legacy row exists

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT kind, content, provenance_id, embedding IS NOT NULL "
            "FROM devbrain.memory WHERE provenance_id = %s",
            (chunk_id,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1, (
        "exactly one memory row expected for the chunk dual-write"
    )
    assert rows[0][0] == "chunk"
    assert rows[0][1] == content
    assert str(rows[0][2]) == chunk_id
    assert rows[0][3] is True  # embedding reused, not recomputed null

    # Guard branch: project_id=None must skip the memory write but
    # still produce a legacy chunk row.
    null_content = f"{TEST_CONTENT_PREFIX}chunk no project"
    null_chunk_id = insert_chunk(
        project_id=None,
        source_type="session",
        source_id=None,
        source_line_start=None,
        source_line_end=None,
        content=null_content,
        embedding=embedding,
        token_count=10,
    )
    assert null_chunk_id  # legacy row still inserted
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM devbrain.memory WHERE provenance_id = %s",
            (null_chunk_id,),
        )
        assert cur.fetchone() is None, (
            "memory.project_id is NOT NULL; the helper must skip dual-"
            "write when project_id is None instead of erroring"
        )


# ─── 7. Idempotency: two dual-writes for the same legacy row → one mem row


def test_idempotency_two_calls_one_row(db):
    """The partial unique index turns retry storms into no-ops.
    First write wins (DO NOTHING); second write's payload is silently
    discarded."""
    pid = _devbrain_project_id(db)
    prov = "55555555-5555-5555-5555-555555555555"
    first = f"{TEST_CONTENT_PREFIX}idempotent first"
    second = f"{TEST_CONTENT_PREFIX}idempotent second"

    with db._conn() as conn, conn.cursor() as cur:
        record_memory(
            cur,
            project_id=pid,
            kind="decision",
            content=first,
            embedding_sql=_embedding_sql(0.6),
            provenance_id=prov,
        )
        record_memory(
            cur,
            project_id=pid,
            kind="decision",
            content=second,
            embedding_sql=_embedding_sql(0.7),
            provenance_id=prov,
        )
        conn.commit()

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT content FROM devbrain.memory "
            "WHERE provenance_id = %s AND kind = 'decision'",
            (prov,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1, (
        f"expected exactly one row after dedup; got {len(rows)}"
    )
    assert rows[0][0] == first  # first write wins under DO NOTHING


# ─── 8. Memory failure does NOT roll back the legacy commit ──────────────


def test_legacy_survives_memory_failure(db, caplog):
    """The savepoint discipline in record_memory is the contract that
    keeps "legacy is source of truth" honest. Force a memory failure
    (CHECK violation on kind) and verify:
        - legacy decision row commits;
        - no orphan memory row;
        - WARNING log captured so operators can see the drop.
    Without the savepoint, psycopg2 would put the transaction in
    InFailedSqlTransaction and the surrounding conn.commit() would
    silently roll back the legacy decision."""
    pid = _devbrain_project_id(db)
    legacy_title = f"{TEST_CONTENT_PREFIX}legacy_title"
    legacy_content = f"{TEST_CONTENT_PREFIX}legacy_content"

    with caplog.at_level(logging.WARNING, logger="memory_writer"):
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO devbrain.decisions "
                "(project_id, title, context, decision) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (pid, legacy_title, legacy_content, legacy_content),
            )
            decision_id = str(cur.fetchone()[0])

            # CHECK constraint forbids kind='not_a_real_kind' — the
            # INSERT inside record_memory raises, the SAVEPOINT
            # rolls back, the cursor is healthy again.
            record_memory(
                cur,
                project_id=pid,
                kind="not_a_real_kind",
                content=legacy_content,
                provenance_id=decision_id,
            )

            # The crucial assertion: this commit MUST succeed despite
            # the failed dual-write above.
            conn.commit()

    # Legacy row persisted.
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM devbrain.decisions WHERE id = %s",
            (decision_id,),
        )
        assert cur.fetchone() is not None, (
            "legacy decision row was rolled back — memory failure "
            "must not poison the legacy commit"
        )
        # No memory row for this provenance.
        cur.execute(
            "SELECT 1 FROM devbrain.memory WHERE provenance_id = %s",
            (decision_id,),
        )
        assert cur.fetchone() is None

    # WARNING captured.
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        "dual-write failed" in r.getMessage() for r in warnings
    ), f"expected dual-write WARNING; got: {[r.getMessage() for r in warnings]}"
