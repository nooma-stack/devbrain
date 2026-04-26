"""Tests for P2.c devbrain.memory backfill (factory/backfill_memory.py).

Each test seeds rows directly into the legacy tables (so the seed
itself doesn't go through the P2.b dual-write path), runs the
backfill, and asserts on the resulting devbrain.memory rows. The
autouse cleanup fixture wipes both legacy and memory rows by content/
title prefix so tests are isolated even if a previous run aborted.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Mirror production sys.path layout: factory/ for config + state_machine
# + backfill_memory; factory/tests has no package __init__ adjustments.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backfill_memory  # noqa: E402
from config import DATABASE_URL  # noqa: E402
from state_machine import FactoryDB  # noqa: E402

# Every seeded row's content/title starts with this prefix so the
# autouse cleanup fixture can wipe them with one LIKE query (works
# for chunk-kind rows whose title is NULL too).
TEST_CONTENT_PREFIX = "backfill_memory_test_"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture(autouse=True)
def _cleanup(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM devbrain.memory WHERE content LIKE %s OR title LIKE %s",
            (f"{TEST_CONTENT_PREFIX}%", f"{TEST_CONTENT_PREFIX}%"),
        )
        cur.execute(
            "DELETE FROM devbrain.chunks WHERE content LIKE %s",
            (f"{TEST_CONTENT_PREFIX}%",),
        )
        cur.execute(
            "DELETE FROM devbrain.decisions "
            "WHERE title LIKE %s OR decision LIKE %s OR context LIKE %s",
            (
                f"{TEST_CONTENT_PREFIX}%",
                f"{TEST_CONTENT_PREFIX}%",
                f"{TEST_CONTENT_PREFIX}%",
            ),
        )
        cur.execute(
            "DELETE FROM devbrain.patterns "
            "WHERE name LIKE %s OR description LIKE %s",
            (f"{TEST_CONTENT_PREFIX}%", f"{TEST_CONTENT_PREFIX}%"),
        )
        cur.execute(
            "DELETE FROM devbrain.issues "
            "WHERE title LIKE %s OR description LIKE %s",
            (f"{TEST_CONTENT_PREFIX}%", f"{TEST_CONTENT_PREFIX}%"),
        )
        cur.execute(
            "DELETE FROM devbrain.raw_sessions WHERE source_hash LIKE %s",
            (f"{TEST_CONTENT_PREFIX}%",),
        )
        conn.commit()


def _devbrain_project_id(db) -> str:
    """The seeded 'devbrain' project (migration 001) — used as a real
    FK target instead of creating a throwaway project per test."""
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.projects WHERE slug = 'devbrain'"
        )
        return str(cur.fetchone()[0])


def _embedding_sql(value: float = 0.0) -> str:
    return "[" + ",".join([str(value)] * 1024) + "]"


def _seed_chunk(
    db,
    *,
    project_id: str | None,
    content: str,
    embedding_value: float = 0.1,
    created_at: datetime | None = None,
) -> str:
    """Direct INSERT into devbrain.chunks (bypasses P2.b dual-write).

    Returns the chunk id. created_at defaults to now() if not given.
    """
    embedding_sql = _embedding_sql(embedding_value)
    with db._conn() as conn, conn.cursor() as cur:
        if created_at is not None:
            cur.execute(
                """
                INSERT INTO devbrain.chunks
                    (project_id, source_type, content, embedding, created_at)
                VALUES (%s, %s, %s, %s::vector, %s)
                RETURNING id
                """,
                (project_id, "session", content, embedding_sql, created_at),
            )
        else:
            cur.execute(
                """
                INSERT INTO devbrain.chunks
                    (project_id, source_type, content, embedding)
                VALUES (%s, %s, %s, %s::vector)
                RETURNING id
                """,
                (project_id, "session", content, embedding_sql),
            )
        chunk_id = str(cur.fetchone()[0])
        conn.commit()
    return chunk_id


def _seed_decision(
    db,
    *,
    project_id: str | None,
    title: str,
    decision_text: str,
    created_at: datetime | None = None,
) -> str:
    with db._conn() as conn, conn.cursor() as cur:
        if created_at is not None:
            cur.execute(
                """
                INSERT INTO devbrain.decisions
                    (project_id, title, context, decision, created_at)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (project_id, title, decision_text, decision_text, created_at),
            )
        else:
            cur.execute(
                """
                INSERT INTO devbrain.decisions
                    (project_id, title, context, decision)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (project_id, title, decision_text, decision_text),
            )
        decision_id = str(cur.fetchone()[0])
        conn.commit()
    return decision_id


def _seed_pattern(
    db, *, project_id: str | None, name: str, description: str,
) -> str:
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO devbrain.patterns (project_id, name, description)
            VALUES (%s, %s, %s) RETURNING id
            """,
            (project_id, name, description),
        )
        pattern_id = str(cur.fetchone()[0])
        conn.commit()
    return pattern_id


def _seed_issue(
    db, *, project_id: str | None, title: str, description: str,
) -> str:
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO devbrain.issues (project_id, title, description)
            VALUES (%s, %s, %s) RETURNING id
            """,
            (project_id, title, description),
        )
        issue_id = str(cur.fetchone()[0])
        conn.commit()
    return issue_id


def _seed_raw_session(
    db,
    *,
    project_id: str | None,
    summary: str | None,
    raw_content: str = "raw transcript",
) -> str:
    """Seed a raw_sessions row. source_hash uses the prefix for cleanup."""
    source_hash = f"{TEST_CONTENT_PREFIX}{uuid.uuid4().hex[:32]}"
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO devbrain.raw_sessions
                (project_id, source_app, source_path, source_hash,
                 raw_content, summary)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                project_id, "test", "/tmp/x", source_hash,
                raw_content, summary,
            ),
        )
        sess_id = str(cur.fetchone()[0])
        conn.commit()
    return sess_id


def _count_memory(
    db, *, kind: str | None = None, provenance_id: str | None = None,
) -> int:
    sql = "SELECT count(*) FROM devbrain.memory WHERE 1=1"
    params: list = []
    if kind is not None:
        sql += " AND kind = %s"
        params.append(kind)
    if provenance_id is not None:
        sql += " AND provenance_id = %s"
        params.append(provenance_id)
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return int(cur.fetchone()[0])


# ─── 1. chunks → kind='chunk' ────────────────────────────────────────────────


def test_backfill_chunks_inserts_to_memory(db):
    pid = _devbrain_project_id(db)
    ids = [
        _seed_chunk(db, project_id=pid, content=f"{TEST_CONTENT_PREFIX}c{i}")
        for i in range(3)
    ]

    counts = backfill_memory.backfill_chunks(db, batch_size=10)

    assert counts["scanned"] >= 3
    assert counts["inserted"] >= 3
    assert counts["batch_failures"] == 0
    for cid in ids:
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT kind, content, title, embedding IS NOT NULL "
                "FROM devbrain.memory WHERE provenance_id = %s",
                (cid,),
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        kind, content, title, has_emb = rows[0]
        assert kind == "chunk"
        assert content.startswith(TEST_CONTENT_PREFIX)
        assert title is None
        assert has_emb is True  # embedding reused, not recomputed


# ─── 2. decisions → kind='decision' ──────────────────────────────────────────


def test_backfill_decisions_uses_correct_kind(db):
    pid = _devbrain_project_id(db)
    d1 = _seed_decision(
        db, project_id=pid,
        title=f"{TEST_CONTENT_PREFIX}decision title 1",
        decision_text=f"{TEST_CONTENT_PREFIX}decision body 1",
    )
    d2 = _seed_decision(
        db, project_id=pid,
        title=f"{TEST_CONTENT_PREFIX}decision title 2",
        decision_text=f"{TEST_CONTENT_PREFIX}decision body 2",
    )

    counts = backfill_memory.backfill_decisions(db, batch_size=10)
    assert counts["inserted"] >= 2

    for did, expected_title, expected_content in [
        (d1, f"{TEST_CONTENT_PREFIX}decision title 1",
         f"{TEST_CONTENT_PREFIX}decision body 1"),
        (d2, f"{TEST_CONTENT_PREFIX}decision title 2",
         f"{TEST_CONTENT_PREFIX}decision body 2"),
    ]:
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT kind, title, content "
                "FROM devbrain.memory WHERE provenance_id = %s",
                (did,),
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "decision"
        assert rows[0][1] == expected_title
        assert rows[0][2] == expected_content


# ─── 3. Idempotent via ON CONFLICT ──────────────────────────────────────────


def test_backfill_idempotent_via_on_conflict(db):
    pid = _devbrain_project_id(db)
    for i in range(5):
        _seed_chunk(db, project_id=pid, content=f"{TEST_CONTENT_PREFIX}idem{i}")

    first = backfill_memory.backfill_chunks(db, batch_size=10)
    assert first["inserted"] >= 5
    first_inserted = first["inserted"]

    second = backfill_memory.backfill_chunks(db, batch_size=10)
    assert second["inserted"] == 0
    # The 5 we just seeded must show up as dups on the second pass.
    # Other concurrent backfills may have added more dups, so use >=.
    assert second["skipped_dup"] >= first_inserted


# ─── 4. project_id NULL → skipped, no memory row ────────────────────────────


def test_backfill_skips_chunks_with_null_project_id(db):
    null_chunk_id = _seed_chunk(
        db, project_id=None,
        content=f"{TEST_CONTENT_PREFIX}null project",
    )

    counts = backfill_memory.backfill_chunks(db, batch_size=10)

    assert counts["skipped_no_project"] >= 1
    assert _count_memory(db, provenance_id=null_chunk_id) == 0


# ─── 5. Historical timestamp preserved ──────────────────────────────────────


def test_backfill_preserves_historical_timestamps(db):
    pid = _devbrain_project_id(db)
    long_ago = datetime.now(timezone.utc) - timedelta(days=30)
    did = _seed_decision(
        db, project_id=pid,
        title=f"{TEST_CONTENT_PREFIX}old decision",
        decision_text=f"{TEST_CONTENT_PREFIX}old body",
        created_at=long_ago,
    )

    backfill_memory.backfill_decisions(db, batch_size=10)

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT created_at FROM devbrain.memory WHERE provenance_id = %s",
            (did,),
        )
        memory_created = cur.fetchone()[0]
    delta = abs((memory_created - long_ago).total_seconds())
    assert delta < 1.0, (
        f"memory.created_at must match legacy.created_at within 1s; "
        f"got delta={delta}s"
    )


# ─── 6. Dry-run does not insert ─────────────────────────────────────────────


def test_backfill_dry_run_does_not_insert(db):
    pid = _devbrain_project_id(db)
    cid = _seed_chunk(
        db, project_id=pid,
        content=f"{TEST_CONTENT_PREFIX}dry run",
    )

    before = _count_memory(db, provenance_id=cid)
    counts = backfill_memory.backfill_chunks(db, dry_run=True, batch_size=10)
    after = _count_memory(db, provenance_id=cid)

    assert before == 0
    assert after == 0  # dry-run inserts nothing
    # Dry-run still reports counts shaped like the live function.
    assert "inserted" in counts
    assert "scanned" in counts
    assert counts["scanned"] >= 1


# ─── 7. backfill_all aggregates ─────────────────────────────────────────────


def test_backfill_all_aggregates_counts(db):
    pid = _devbrain_project_id(db)
    _seed_chunk(db, project_id=pid, content=f"{TEST_CONTENT_PREFIX}all c")
    _seed_decision(
        db, project_id=pid,
        title=f"{TEST_CONTENT_PREFIX}all d",
        decision_text=f"{TEST_CONTENT_PREFIX}all d body",
    )
    _seed_pattern(
        db, project_id=pid,
        name=f"{TEST_CONTENT_PREFIX}all p",
        description=f"{TEST_CONTENT_PREFIX}all p desc",
    )
    _seed_issue(
        db, project_id=pid,
        title=f"{TEST_CONTENT_PREFIX}all i",
        description=f"{TEST_CONTENT_PREFIX}all i desc",
    )
    _seed_raw_session(
        db, project_id=pid,
        summary=f"{TEST_CONTENT_PREFIX}all rs summary",
    )

    results = backfill_memory.backfill_all(db, batch_size=10)

    for label in ("chunks", "decisions", "patterns", "issues", "raw_sessions"):
        assert label in results, f"missing {label} in results: {list(results)}"
    assert "TOTAL" in results

    total = results["TOTAL"]
    # Sum across legacy tables must equal TOTAL.scanned/.inserted.
    summed_scanned = sum(
        results[label]["scanned"]
        for label in ("chunks", "decisions", "patterns", "issues", "raw_sessions")
    )
    summed_inserted = sum(
        results[label]["inserted"]
        for label in ("chunks", "decisions", "patterns", "issues", "raw_sessions")
    )
    assert total["scanned"] == summed_scanned
    assert total["inserted"] == summed_inserted
    # We seeded one row per legacy table → at least 5 inserts.
    assert total["inserted"] >= 5


# ─── 8. raw_sessions → kind='session_summary' ───────────────────────────────


def test_backfill_session_summary_kind(db):
    pid = _devbrain_project_id(db)
    summary = f"{TEST_CONTENT_PREFIX}" + "x" * 200  # > 80 chars
    sess_id = _seed_raw_session(db, project_id=pid, summary=summary)

    counts = backfill_memory.backfill_raw_sessions(db, batch_size=10)
    assert counts["inserted"] >= 1

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT kind, title, content "
            "FROM devbrain.memory WHERE provenance_id = %s",
            (sess_id,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    kind, title, content = rows[0]
    assert kind == "session_summary"
    assert title == summary[:80]
    assert content == summary


# ─── 9. (bonus) raw_sessions with NULL summary skipped ──────────────────────


def test_backfill_skips_raw_session_with_null_summary(db):
    pid = _devbrain_project_id(db)
    sess_id = _seed_raw_session(db, project_id=pid, summary=None)

    counts = backfill_memory.backfill_raw_sessions(db, batch_size=10)

    assert counts["skipped_no_summary"] >= 1
    assert _count_memory(db, provenance_id=sess_id) == 0


# ─── 10. (bonus) end-to-end re-run inserts zero ─────────────────────────────


def test_backfill_all_then_rerun_inserts_zero(db):
    pid = _devbrain_project_id(db)
    _seed_chunk(db, project_id=pid, content=f"{TEST_CONTENT_PREFIX}rerun c")
    _seed_decision(
        db, project_id=pid,
        title=f"{TEST_CONTENT_PREFIX}rerun d",
        decision_text=f"{TEST_CONTENT_PREFIX}rerun d body",
    )
    _seed_pattern(
        db, project_id=pid,
        name=f"{TEST_CONTENT_PREFIX}rerun p",
        description=f"{TEST_CONTENT_PREFIX}rerun p desc",
    )
    _seed_issue(
        db, project_id=pid,
        title=f"{TEST_CONTENT_PREFIX}rerun i",
        description=f"{TEST_CONTENT_PREFIX}rerun i desc",
    )
    _seed_raw_session(
        db, project_id=pid,
        summary=f"{TEST_CONTENT_PREFIX}rerun rs summary",
    )

    backfill_memory.backfill_all(db, batch_size=10)
    second = backfill_memory.backfill_all(db, batch_size=10)
    assert second["TOTAL"]["inserted"] == 0
