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
import uuid
from pathlib import Path

import pytest

# Mirror the production sys.path layout: factory/ for config, ingest/
# for memory_writer + insert_chunk.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent.parent / "ingest")
)

from db import delete_session, insert_chunk, insert_raw_session  # noqa: E402  (ingest/db.py)
from memory_writer import record_memory  # noqa: E402  (ingest/memory_writer.py)
from state_machine import FactoryDB  # noqa: E402

# All test rows have content starting with this prefix so the autouse
# cleanup fixture can wipe them with one LIKE query (works even for
# chunk-kind memory rows whose title is NULL).
TEST_CONTENT_PREFIX = "dual_write_test_"


@pytest.fixture
def db(database_url):
    return FactoryDB(database_url)


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


def test_memory_unique_indexes_applied(db):
    """The dual-write helper's ON CONFLICT clauses rely on two
    migration-037 indexes:

      idx_memory_session_summary_unique on (provenance_id, kind)
        WHERE kind='session_summary'
      idx_memory_atom_title_unique on (provenance_id, kind, title)
        WHERE kind IN ('pattern','decision','lesson','issue')

    If either is missing, the inferred-constraint ON CONFLICT in
    record_memory will error. Surface that as a clear failure here.
    """
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM devbrain.schema_migrations "
            "WHERE filename = %s",
            ("037_atom_provenance_linkage.sql",),
        )
        assert cur.fetchone() is not None, (
            "037_atom_provenance_linkage.sql is not recorded in "
            "schema_migrations — run `bin/devbrain migrate`"
        )

        cur.execute(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname = 'devbrain' AND tablename = 'memory' "
            "AND indexname IN ('idx_memory_session_summary_unique', "
            "                  'idx_memory_atom_title_unique')"
        )
        rows = {r[0]: r[1] for r in cur.fetchall()}

        assert "idx_memory_session_summary_unique" in rows, (
            "session_summary unique index missing"
        )
        sumdef = rows["idx_memory_session_summary_unique"]
        assert "UNIQUE" in sumdef.upper()
        assert "provenance_id" in sumdef
        assert "session_summary" in sumdef

        assert "idx_memory_atom_title_unique" in rows, (
            "atom title-aware unique index missing"
        )
        atomdef = rows["idx_memory_atom_title_unique"]
        assert "UNIQUE" in atomdef.upper()
        assert "title" in atomdef
        # The predicate enumerates atom kinds; presence of any of them
        # is enough for the smoke test.
        assert any(
            kind in atomdef for kind in ("pattern", "decision", "lesson")
        ), f"atom index predicate doesn't reference atom kinds: {atomdef}"

        # Migration 045: the chunk dual-write's ON CONFLICT relies on a
        # partial unique index keyed on (provenance_id, kind, md5(content)).
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = 'devbrain' AND tablename = 'memory' "
            "AND indexname = 'idx_memory_chunk_dedup_unique'"
        )
        chunkrow = cur.fetchone()
        assert chunkrow is not None, (
            "idx_memory_chunk_dedup_unique missing — run "
            "`bin/devbrain migrate` (migration 045)"
        )
        chunkdef = chunkrow[0]
        assert "UNIQUE" in chunkdef.upper()
        assert "provenance_id" in chunkdef
        assert "md5" in chunkdef.lower()
        assert "chunk" in chunkdef


def test_dual_write_chunk_is_idempotent(db):
    """Re-running the chunk dual-write for the same (provenance_id,
    content) must NOT create a second memory row — migration 045's
    ON CONFLICT (provenance_id, kind, md5(content)) DO NOTHING. This is
    the guard whose absence let BrightBrain accumulate 257k duplicate
    chunk rows (one chunk had 824 copies)."""
    pid = _devbrain_project_id(db)
    prov = "55555555-5555-5555-5555-555555555555"
    content = f"{TEST_CONTENT_PREFIX}chunk idempotent body"
    emb = _embedding_sql(0.5)

    # Two separate transactions, same natural key — simulates a backfill
    # re-run over an already-ingested chunk.
    for _ in range(2):
        with db._conn() as conn, conn.cursor() as cur:
            record_memory(
                cur,
                project_id=pid,
                kind="chunk",
                content=content,
                embedding_sql=emb,
                provenance_id=prov,
            )
            conn.commit()

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM devbrain.memory WHERE content = %s",
            (content,),
        )
        (n,) = cur.fetchone()
    assert n == 1, f"chunk dual-write not idempotent: {n} rows for one key"

    # Different content under the SAME provenance (session) is a distinct
    # chunk and MUST still insert — provenance alone is not the key.
    content2 = f"{TEST_CONTENT_PREFIX}chunk idempotent body TWO"
    with db._conn() as conn, conn.cursor() as cur:
        record_memory(
            cur, project_id=pid, kind="chunk", content=content2,
            embedding_sql=emb, provenance_id=prov,
        )
        conn.commit()
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM devbrain.memory "
            "WHERE provenance_id = %s AND content LIKE %s",
            (prov, f"{TEST_CONTENT_PREFIX}chunk idempotent body%"),
        )
        (n2,) = cur.fetchone()
    assert n2 == 2, (
        "distinct content under one provenance must remain distinct rows"
    )


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


def test_dual_write_chunk_provenance_is_source_session(db):
    """Migration 032 fixed the bug where chunk dual-writes used
    `provenance_id = chunk_id` (the chunk row's own UUID) instead of
    `provenance_id = chunks.source_id` (the originating session UUID).

    The new contract: when source_id is supplied, the memory row's
    provenance_id IS that source_id. When source_id is None (e.g.
    markdown imports), provenance_id is NULL.
    """
    pid = _devbrain_project_id(db)
    embedding = [0.5] * 1024

    # Case A: source_id present → memory.provenance_id == source_id
    source_session_uuid = "77777777-7777-7777-7777-777777777777"
    content_a = f"{TEST_CONTENT_PREFIX}chunk with source_id"
    chunk_id_a = insert_chunk(
        project_id=pid,
        source_type="session",
        source_id=source_session_uuid,
        source_line_start=None,
        source_line_end=None,
        content=content_a,
        embedding=embedding,
        token_count=10,
    )
    assert chunk_id_a

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT kind, provenance_id FROM devbrain.memory "
            "WHERE content = %s",
            (content_a,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "chunk"
    assert str(rows[0][1]) == source_session_uuid, (
        "chunk dual-write must set provenance_id = source_id "
        "(the source session's UUID), not the chunk row's own UUID"
    )

    # Case B: source_id=None → memory.provenance_id IS NULL
    content_b = f"{TEST_CONTENT_PREFIX}chunk no source"
    chunk_id_b = insert_chunk(
        project_id=pid,
        source_type="session",
        source_id=None,
        source_line_start=None,
        source_line_end=None,
        content=content_b,
        embedding=embedding,
        token_count=10,
    )
    assert chunk_id_b

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT provenance_id FROM devbrain.memory WHERE content = %s",
            (content_b,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] is None, (
        "chunk with no source_id should produce memory row with "
        "NULL provenance_id (no session to attribute to)"
    )

    # Case C: project_id=None — must skip the memory write but still
    # produce a legacy chunk row (existing guard preserved).
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
            "SELECT 1 FROM devbrain.memory WHERE content = %s",
            (null_content,),
        )
        assert cur.fetchone() is None, (
            "memory.project_id is NOT NULL; the helper must skip dual-"
            "write when project_id is None instead of erroring"
        )


def test_dual_write_chunks_share_session_provenance(db):
    """After migration 032, multiple chunks for the same source session
    legitimately share one provenance_id (and the partial unique index
    no longer blocks this for kind='chunk')."""
    pid = _devbrain_project_id(db)
    embedding = [0.5] * 1024
    shared_session = "88888888-8888-8888-8888-888888888888"

    contents = [
        f"{TEST_CONTENT_PREFIX}shared session chunk 1",
        f"{TEST_CONTENT_PREFIX}shared session chunk 2",
        f"{TEST_CONTENT_PREFIX}shared session chunk 3",
    ]
    for c in contents:
        insert_chunk(
            project_id=pid,
            source_type="session",
            source_id=shared_session,
            source_line_start=None,
            source_line_end=None,
            content=c,
            embedding=embedding,
            token_count=10,
        )

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory "
            "WHERE provenance_id = %s AND kind = 'chunk'",
            (shared_session,),
        )
        count = cur.fetchone()[0]
    assert count == 3, (
        f"expected 3 chunks sharing the same session provenance_id, "
        f"got {count} — partial unique index may be incorrectly blocking "
        f"multiple chunks per session"
    )


# ─── 7. Idempotency: two dual-writes for the same legacy row → one mem row


def test_idempotency_two_calls_one_row(db):
    """Two writes with the same (provenance_id, kind, title) collapse
    to one row via ON CONFLICT DO NOTHING. The first write wins;
    second write's payload is silently discarded. (Different titles
    are not deduped — see test_atom_many_titles_coexist_per_session.)"""
    pid = _devbrain_project_id(db)
    prov = "55555555-5555-5555-5555-555555555555"
    same_title = f"{TEST_CONTENT_PREFIX}idempotent title"
    first = f"{TEST_CONTENT_PREFIX}idempotent first"
    second = f"{TEST_CONTENT_PREFIX}idempotent second"

    with db._conn() as conn, conn.cursor() as cur:
        record_memory(
            cur,
            project_id=pid,
            kind="decision",
            content=first,
            title=same_title,
            embedding_sql=_embedding_sql(0.6),
            provenance_id=prov,
        )
        record_memory(
            cur,
            project_id=pid,
            kind="decision",
            content=second,
            title=same_title,
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
    # First write wins.
    assert rows[0][0] == first


def test_atom_many_titles_coexist_per_session(db):
    """Migration 037 relaxed the atom uniqueness from (provenance_id,
    kind) to (provenance_id, kind, title). A session may produce many
    atoms of the same kind — each with a distinct title."""
    pid = _devbrain_project_id(db)
    shared_session = "66666666-6666-6666-6666-666666666666"

    for i in range(3):
        with db._conn() as conn, conn.cursor() as cur:
            record_memory(
                cur,
                project_id=pid,
                kind="decision",
                content=f"{TEST_CONTENT_PREFIX}decision body {i}",
                title=f"{TEST_CONTENT_PREFIX}decision title {i}",
                embedding_sql=_embedding_sql(0.5),
                provenance_id=shared_session,
            )
            conn.commit()

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory "
            "WHERE provenance_id = %s AND kind = 'decision'",
            (shared_session,),
        )
        count = cur.fetchone()[0]
    assert count == 3, (
        f"expected 3 distinct-title decisions to coexist on one session; "
        f"got {count} — the (provenance_id, kind, title) unique index "
        f"may not be matching ON CONFLICT correctly"
    )


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


# ─── delete_session() helper — prevents the 2026-04-09-style bug ─────────────


def test_delete_session_removes_session_chunks_and_memory(db):
    """delete_session() atomically cleans up a raw_session, all its
    chunks, and the corresponding memory rows (since none of those
    edges has ON DELETE CASCADE). Replaces ad-hoc cleanup scripts."""
    pid = _devbrain_project_id(db)
    embedding = [0.1] * 1024
    session_uuid = "99999999-9999-9999-9999-999999999999"

    # Seed: 1 raw_session row + 3 chunks (which dual-write into memory).
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO devbrain.raw_sessions (
                id, project_id, source_app, source_path, source_hash,
                raw_content
            ) VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_app, source_hash) DO NOTHING
            """,
            (
                session_uuid, pid, "claude_code",
                "test://delete_session", "delete_session_test_hash",
                f"{TEST_CONTENT_PREFIX}raw content",
            ),
        )
        conn.commit()

    for i in range(3):
        insert_chunk(
            project_id=pid,
            source_type="session",
            source_id=session_uuid,
            source_line_start=i * 10,
            source_line_end=(i + 1) * 10,
            content=f"{TEST_CONTENT_PREFIX}session chunk {i}",
            embedding=embedding,
            token_count=10,
        )

    # Pre-condition: rows exist in all three tables for this session.
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.chunks WHERE source_id = %s",
            (session_uuid,),
        )
        assert cur.fetchone()[0] == 3
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory WHERE provenance_id = %s",
            (session_uuid,),
        )
        assert cur.fetchone()[0] == 3
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.raw_sessions WHERE id = %s",
            (session_uuid,),
        )
        assert cur.fetchone()[0] == 1

    # Act.
    result = delete_session(session_uuid)
    assert result == {"memory": 3, "chunks": 3, "raw_sessions": 1}, (
        f"expected per-table counts to be 3/3/1; got {result}"
    )

    # Post-condition: nothing left.
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.chunks WHERE source_id = %s",
            (session_uuid,),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory WHERE provenance_id = %s",
            (session_uuid,),
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.raw_sessions WHERE id = %s",
            (session_uuid,),
        )
        assert cur.fetchone()[0] == 0


def test_delete_session_is_safe_when_session_missing(db):
    """Best-effort contract: deleting a session that doesn't exist
    returns all-zeros and does not raise. Lets callers idempotently
    retry."""
    nonexistent = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    result = delete_session(nonexistent)
    assert result == {"memory": 0, "chunks": 0, "raw_sessions": 0}


def test_session_ingest_transaction_is_atomic(db):
    """insert_raw_session + insert_chunk share a caller cursor and commit
    together. A rolled-back ingest leaves NOTHING — so the source-hash gate
    re-ingests the session cleanly instead of stranding a half-indexed one."""
    pid = _devbrain_project_id(db)
    shash = f"atomic_test_{uuid.uuid4().hex}"
    content = f"{TEST_CONTENT_PREFIX}atomic chunk"

    def _ingest(commit: bool) -> str:
        conn = db._conn()
        try:
            with conn.cursor() as cur:
                sid = insert_raw_session(
                    project_id=pid, source_app="claude_code",
                    source_path="test://atomic", source_hash=shash,
                    session_id=None, model_used=None, started_at=None,
                    ended_at=None, message_count=1,
                    raw_content=f"{TEST_CONTENT_PREFIX}raw", summary=None,
                    files_touched=[], cur=cur,
                )
                assert sid
                insert_chunk(
                    project_id=pid, source_type="session", source_id=sid,
                    source_line_start=0, source_line_end=1, content=content,
                    embedding=[0.1] * 1024, token_count=3, cur=cur,
                )
            conn.commit() if commit else conn.rollback()
            return sid
        finally:
            conn.close()

    # Rolled back → nothing in any of the three tables.
    _ingest(commit=False)
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM devbrain.raw_sessions WHERE source_hash=%s", (shash,)
        )
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM devbrain.memory WHERE content=%s", (content,))
        assert cur.fetchone()[0] == 0

    # Committed → raw_session + chunk + dual-written memory all land together.
    sid = _ingest(commit=True)
    try:
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM devbrain.memory WHERE content=%s AND kind='chunk'",
                (content,),
            )
            assert cur.fetchone()[0] == 1
            cur.execute(
                "SELECT count(*) FROM devbrain.raw_sessions WHERE source_hash=%s", (shash,)
            )
            assert cur.fetchone()[0] == 1
    finally:
        delete_session(sid)
