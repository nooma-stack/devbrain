"""Tests for P2.d.i — switch read paths from legacy tables to devbrain.memory.

Mirrors the test_dual_write.py setup: rows are content-prefixed so a
single LIKE-cleanup fixture wipes them, and the seeded 'devbrain'
project is used as the FK target instead of a throwaway project per
test.

Two halves:

  - The factory/learning.py read path (get_review_lessons +
    _store_lessons dedup) is exercised in-process via the imported
    function, with caplog asserting the dual-write drift WARNING fires
    when memory is empty but legacy has rows.

  - The mcp-server/src/index.ts read path (deep_search and
    health_check) is verified by issuing the same SQL shape via
    psycopg2 — there is no Python test harness for the TS server.
    The asserted invariants (memory rows visible, archived rows hidden,
    legacy-only rows hidden, codebase falls back to legacy chunks) are
    the contract those queries are meant to enforce, regardless of
    transport.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

# Mirror the production sys.path layout.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent.parent / "ingest")
)

from config import DATABASE_URL  # noqa: E402  (factory/config.py)
from state_machine import FactoryDB  # noqa: E402

import learning  # noqa: E402  (factory/learning.py)

TEST_CONTENT_PREFIX = "read_switch_test_"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


_THROWAWAY_PROJECT_SLUG = f"{TEST_CONTENT_PREFIX}project"


@pytest.fixture(autouse=True)
def _cleanup(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        # Order matters — child rows (memory/chunks/patterns/etc.) carry
        # FKs to projects, so wipe content-prefixed rows first then drop
        # the throwaway project last.
        cur.execute(
            "DELETE FROM devbrain.memory WHERE content LIKE %s",
            (f"{TEST_CONTENT_PREFIX}%",),
        )
        cur.execute(
            "DELETE FROM devbrain.chunks WHERE content LIKE %s",
            (f"{TEST_CONTENT_PREFIX}%",),
        )
        cur.execute(
            "DELETE FROM devbrain.patterns WHERE description LIKE %s",
            (f"{TEST_CONTENT_PREFIX}%",),
        )
        cur.execute(
            "DELETE FROM devbrain.decisions "
            "WHERE title LIKE %s OR context LIKE %s",
            (f"{TEST_CONTENT_PREFIX}%", f"{TEST_CONTENT_PREFIX}%"),
        )
        cur.execute(
            "DELETE FROM devbrain.issues WHERE description LIKE %s",
            (f"{TEST_CONTENT_PREFIX}%",),
        )
        # Wipe any rows still attached to the throwaway project before
        # the project DELETE — the FK is RESTRICT, not CASCADE.
        cur.execute(
            "SELECT id FROM devbrain.projects WHERE slug = %s",
            (_THROWAWAY_PROJECT_SLUG,),
        )
        row = cur.fetchone()
        if row is not None:
            tp_id = row[0]
            for table in (
                "memory", "chunks", "patterns", "decisions", "issues",
            ):
                cur.execute(
                    f"DELETE FROM devbrain.{table} WHERE project_id = %s",
                    (tp_id,),
                )
            cur.execute(
                "DELETE FROM devbrain.projects WHERE id = %s", (tp_id,),
            )
        conn.commit()


def _devbrain_project_id(db) -> str:
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.projects WHERE slug = 'devbrain'"
        )
        return cur.fetchone()[0]


def _throwaway_project_id(db) -> str:
    """A test-only project so dual-write-drift tests can guarantee an
    empty starting state — the seeded 'devbrain' project carries real
    memory rows from production usage that would mask the
    "memory empty + legacy non-empty" condition."""
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.projects WHERE slug = %s",
            (_THROWAWAY_PROJECT_SLUG,),
        )
        row = cur.fetchone()
        if row is not None:
            return row[0]
        cur.execute(
            """INSERT INTO devbrain.projects (slug, name, description)
               VALUES (%s, %s, %s)
               RETURNING id""",
            (
                _THROWAWAY_PROJECT_SLUG,
                "Read-switch test project",
                "Created by test_read_switch_to_memory.py — safe to drop.",
            ),
        )
        pid = cur.fetchone()[0]
        conn.commit()
        return pid


def _embedding_sql(value: float = 0.0) -> str:
    return "[" + ",".join([str(value)] * 1024) + "]"


def _seed_memory(
    db,
    *,
    project_id: str,
    kind: str,
    content: str,
    title: str | None = None,
    embedding_value: float = 0.1,
    archived: bool = False,
    applies_when: str | None = None,
    provenance_id: str | None = None,
) -> str:
    """Insert a memory row directly (bypassing record_memory) so the
    seed itself doesn't go through the dual-write path under test."""
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO devbrain.memory
                   (project_id, kind, title, content, embedding,
                    applies_when, provenance_id, archived_at)
               VALUES (%s, %s, %s, %s, %s::vector, %s::jsonb, %s,
                       CASE WHEN %s THEN now() ELSE NULL END)
               RETURNING id""",
            (
                project_id,
                kind,
                title,
                content,
                _embedding_sql(embedding_value),
                applies_when,
                provenance_id,
                archived,
            ),
        )
        memory_id = str(cur.fetchone()[0])
        conn.commit()
        return memory_id


# ─── 1. deep_search reads from devbrain.memory ───────────────────────────


def test_deep_search_reads_from_memory_table(db):
    """The deep_search query in mcp-server/src/index.ts must surface
    rows that live ONLY in devbrain.memory (no legacy chunks row)."""
    pid = _devbrain_project_id(db)
    content = f"{TEST_CONTENT_PREFIX}deep_search seed body"
    _seed_memory(
        db,
        project_id=pid,
        kind="decision",
        title=f"{TEST_CONTENT_PREFIX}seed title",
        content=content,
        embedding_value=0.1,
    )

    # Issue the same SQL shape deep_search runs (memory + LEFT JOIN
    # chunks). With no legacy chunk for this row, the LEFT JOIN cell
    # is NULL — the row must still come back.
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT m.id, m.kind, m.content, c.id as legacy_chunk_id
               FROM devbrain.memory m
               JOIN devbrain.projects p ON m.project_id = p.id
               LEFT JOIN devbrain.chunks c
                 ON m.kind = 'chunk' AND c.id = m.provenance_id
               WHERE m.embedding IS NOT NULL
                 AND m.archived_at IS NULL
                 AND m.project_id = %s
                 AND m.content = %s""",
            (pid, content),
        )
        rows = cur.fetchall()

    assert len(rows) == 1
    _, kind, body, legacy_chunk_id = rows[0]
    assert kind == "decision"
    assert body == content
    assert legacy_chunk_id is None


# ─── 2. get_review_lessons reads from memory, not legacy patterns ────────


def test_get_review_lessons_reads_from_memory(db):
    """Lessons live in memory; legacy-only patterns rows must NOT
    surface (they would, pre-switch — that's the regression we guard
    against)."""
    pid = _devbrain_project_id(db)
    in_memory = (
        f"{TEST_CONTENT_PREFIX}lesson in memory\n\nContext: applies "
        "to thing"
    )
    _seed_memory(
        db,
        project_id=pid,
        kind="pattern",
        title=f"{TEST_CONTENT_PREFIX}memory lesson",
        content=in_memory,
        embedding_value=0.2,
        applies_when='{"category": "factory_review"}',
    )

    # Insert a legacy-only patterns row WITHOUT a corresponding memory
    # row. Pre-switch this would have come back; post-switch it must
    # be invisible to get_review_lessons.
    legacy_only = (
        f"{TEST_CONTENT_PREFIX}legacy-only lesson\n\nContext: nope"
    )
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO devbrain.patterns
                   (project_id, name, category, description, tags)
               VALUES (%s, %s, %s, %s, %s)""",
            (
                pid,
                f"{TEST_CONTENT_PREFIX}legacy",
                "factory_review",
                legacy_only,
                "[]",
            ),
        )
        conn.commit()

    lessons = learning.get_review_lessons(pid, limit=50)

    assert in_memory in lessons
    assert legacy_only not in lessons


# ─── 3. archived rows are excluded ───────────────────────────────────────


def test_archived_rows_excluded_from_reads(db):
    """`archived_at IS NULL` is the soft-delete signal for Phase 6 and
    must filter out archived rows on every read path."""
    pid = _devbrain_project_id(db)
    live_content = f"{TEST_CONTENT_PREFIX}live lesson\n\nContext: live"
    archived_content = (
        f"{TEST_CONTENT_PREFIX}archived lesson\n\nContext: gone"
    )

    _seed_memory(
        db,
        project_id=pid,
        kind="pattern",
        title=f"{TEST_CONTENT_PREFIX}live",
        content=live_content,
        embedding_value=0.3,
        applies_when='{"category": "factory_review"}',
    )
    _seed_memory(
        db,
        project_id=pid,
        kind="pattern",
        title=f"{TEST_CONTENT_PREFIX}archived",
        content=archived_content,
        embedding_value=0.4,
        archived=True,
        applies_when='{"category": "factory_review"}',
    )

    lessons = learning.get_review_lessons(pid, limit=50)

    assert live_content in lessons
    assert archived_content not in lessons

    # Also sanity-check the deep_search WHERE clause: archived rows
    # must not surface even when they would otherwise match.
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT m.content
               FROM devbrain.memory m
               WHERE m.embedding IS NOT NULL
                 AND m.archived_at IS NULL
                 AND m.project_id = %s
                 AND m.content LIKE %s""",
            (pid, f"{TEST_CONTENT_PREFIX}%"),
        )
        contents = {row[0] for row in cur.fetchall()}
    assert live_content in contents
    assert archived_content not in contents


# ─── 4. dual-write drift WARNING fires when memory empty + legacy non-empty


def test_zero_results_with_legacy_data_logs_warning(db, caplog):
    """If get_review_lessons returns zero rows from memory but the
    legacy patterns table has factory_review rows, log a WARNING with
    "dual-write drift" so operators can rerun backfill before P2.d.ii
    drops legacy.

    Uses a throwaway project so we can guarantee memory starts empty
    for that project — the seeded 'devbrain' project has real memory
    lessons from production use that would otherwise mask the drift
    condition.
    """
    pid = _throwaway_project_id(db)

    # Legacy-only seed: a factory_review pattern with no matching
    # memory row.
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO devbrain.patterns
                   (project_id, name, category, description, tags)
               VALUES (%s, %s, %s, %s, %s)""",
            (
                pid,
                f"{TEST_CONTENT_PREFIX}legacy_only",
                "factory_review",
                f"{TEST_CONTENT_PREFIX}legacy_only desc\n\nContext: x",
                "[]",
            ),
        )
        conn.commit()

    with caplog.at_level(logging.WARNING, logger="learning"):
        lessons = learning.get_review_lessons(pid, limit=50)

    assert lessons == []
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        "dual-write drift" in r.getMessage() for r in warnings
    ), (
        f"expected dual-write drift WARNING; got: "
        f"{[r.getMessage() for r in warnings]}"
    )


# ─── 5. health_check reports memory + legacy counts ──────────────────────


def test_health_check_reports_memory_counts(db):
    """The new health_check tool must report storage.memory.* and
    storage.legacy.* counts. Verified by issuing the same per-table
    COUNT(*) queries the tool runs."""
    pid = _devbrain_project_id(db)

    # Seed one memory row of each kind.
    for kind, value in (
        ("chunk", 0.5),
        ("decision", 0.51),
        ("pattern", 0.52),
        ("issue", 0.53),
        ("session_summary", 0.54),
    ):
        _seed_memory(
            db,
            project_id=pid,
            kind=kind,
            content=f"{TEST_CONTENT_PREFIX}health {kind}",
            embedding_value=value,
        )

    # Snapshot counts (project-scoped, mirroring health_check's $1
    # filter shape).
    with db._conn() as conn, conn.cursor() as cur:
        out: dict = {"memory": {}, "legacy": {}}
        for label, kind in (
            ("chunks", "chunk"),
            ("decisions", "decision"),
            ("patterns", "pattern"),
            ("issues", "issue"),
            ("session_summaries", "session_summary"),
        ):
            cur.execute(
                "SELECT COUNT(*) FROM devbrain.memory "
                "WHERE kind = %s AND project_id = %s",
                (kind, pid),
            )
            out["memory"][label] = cur.fetchone()[0]
        for label, table in (
            ("chunks", "chunks"),
            ("decisions", "decisions"),
            ("patterns", "patterns"),
            ("issues", "issues"),
            ("raw_sessions", "raw_sessions"),
        ):
            cur.execute(
                f"SELECT COUNT(*) FROM devbrain.{table} "
                "WHERE project_id = %s",
                (pid,),
            )
            out["legacy"][label] = cur.fetchone()[0]

    # Each seeded kind contributes one memory row; archived rows would
    # still count (health_check intentionally reports raw counts so
    # operators see the full picture).
    assert out["memory"]["chunks"] >= 1
    assert out["memory"]["decisions"] >= 1
    assert out["memory"]["patterns"] >= 1
    assert out["memory"]["issues"] >= 1
    assert out["memory"]["session_summaries"] >= 1
    # Schema sanity: every legacy key the tool reports must exist.
    assert "raw_sessions" in out["legacy"]


# ─── 6. kind filter isolates chunks from decisions ───────────────────────


def test_kind_filter_isolates_chunks_from_decisions(db):
    """deep_search filters memory.kind via ANY($kinds). Filtering to
    decision must NOT return chunk-kind rows even if both have higher
    scores than the unfiltered universe."""
    pid = _devbrain_project_id(db)
    chunk_content = f"{TEST_CONTENT_PREFIX}filter chunk body"
    decision_content = f"{TEST_CONTENT_PREFIX}filter decision body"

    _seed_memory(
        db, project_id=pid, kind="chunk",
        content=chunk_content, embedding_value=0.6,
    )
    _seed_memory(
        db, project_id=pid, kind="decision",
        title=f"{TEST_CONTENT_PREFIX}filter decision title",
        content=decision_content, embedding_value=0.6,
    )

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT m.kind, m.content
               FROM devbrain.memory m
               JOIN devbrain.projects p ON m.project_id = p.id
               WHERE m.embedding IS NOT NULL
                 AND m.archived_at IS NULL
                 AND m.project_id = %s
                 AND m.kind = ANY(%s)
                 AND m.content LIKE %s""",
            (pid, ["decision"], f"{TEST_CONTENT_PREFIX}%"),
        )
        rows = cur.fetchall()

    kinds = {r[0] for r in rows}
    contents = {r[1] for r in rows}
    assert kinds == {"decision"}
    assert decision_content in contents
    assert chunk_content not in contents


# ─── 7. codebase falls back to legacy chunks ─────────────────────────────


def test_codebase_falls_back_to_legacy_chunks(db):
    """codebase_index ingest never landed in P2.b's dual-write, so
    deep_search keeps a legacy-only branch for source_type='codebase'.
    A legacy chunks row with source_type='codebase' must come back via
    that branch, even though no memory row exists for it."""
    pid = _devbrain_project_id(db)
    codebase_content = f"{TEST_CONTENT_PREFIX}codebase fallback body"

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO devbrain.chunks
                   (project_id, source_type, source_id,
                    source_line_start, source_line_end, content,
                    embedding, token_count)
               VALUES (%s, 'codebase', NULL, 1, 1, %s, %s::vector, 1)
               RETURNING id""",
            (pid, codebase_content, _embedding_sql(0.7)),
        )
        chunk_id = str(cur.fetchone()[0])
        conn.commit()

    # The codebase-fallback SQL deep_search runs.
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT c.id, c.source_type, c.content
               FROM devbrain.chunks c
               JOIN devbrain.projects p ON c.project_id = p.id
               WHERE c.embedding IS NOT NULL
                 AND c.source_type = 'codebase'
                 AND c.project_id = %s
                 AND c.content = %s""",
            (pid, codebase_content),
        )
        rows = cur.fetchall()

    assert len(rows) == 1
    assert str(rows[0][0]) == chunk_id
    assert rows[0][1] == "codebase"
    assert rows[0][2] == codebase_content

    # And: the codebase chunk has NO memory dual-write (the row would
    # only exist if codebase_indexer started using insert_chunk's
    # dual-write — which P2.e tracks separately). Sanity-asserting the
    # absence here keeps the fallback branch load-bearing.
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM devbrain.memory WHERE provenance_id = %s",
            (chunk_id,),
        )
        assert cur.fetchone() is None
