"""Schema verification tests for migrations/010_unified_memory.sql.

Pure schema tests — no adapters or behavior under test. We INSERT
through psycopg2 directly to verify defaults, constraints, FKs, and
indexes are wired exactly as the migration declares.
"""
from __future__ import annotations

from decimal import Decimal

import psycopg2
import psycopg2.errors
import pytest

from config import DATABASE_URL
from state_machine import FactoryDB

# All test-created rows have content starting with this prefix so the
# cleanup fixture can wipe them with one LIKE query (works even for
# chunk-kind rows which have title=NULL).
TEST_CONTENT_PREFIX = "memory_table_test_"


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
        conn.commit()


def _devbrain_project_id(db) -> str:
    """The seeded 'devbrain' project (migration 001) — use as a real
    FK target instead of creating a throwaway project per test."""
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM devbrain.projects WHERE slug = 'devbrain'")
        return cur.fetchone()[0]


# ─── 1. Table exists ─────────────────────────────────────────────────────────


def test_memory_table_exists(db):
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'devbrain' AND table_name = 'memory'"
        )
        assert cur.fetchone() is not None


# ─── 2. Required columns w/ types & nullability ──────────────────────────────


def test_memory_required_columns(db):
    expected = {
        "id":            ("uuid", "NO"),
        "project_id":    ("uuid", "NO"),
        "kind":          ("text", "NO"),
        "title":         ("text", "YES"),
        "content":       ("text", "NO"),
        # `embedding` is a pgvector type — information_schema reports
        # data_type='USER-DEFINED'; check udt_name='vector' separately.
        "embedding":     ("USER-DEFINED", "YES"),
        "strength":      ("numeric", "NO"),
        "hit_count":     ("integer", "NO"),
        "last_hit":      ("timestamp with time zone", "YES"),
        "applies_when":  ("jsonb", "YES"),
        "provenance_id": ("uuid", "YES"),
        "tier":          ("text", "NO"),
        "archived_at":   ("timestamp with time zone", "YES"),
        "created_at":    ("timestamp with time zone", "NO"),
        "updated_at":    ("timestamp with time zone", "NO"),
    }
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, is_nullable, udt_name "
            "FROM information_schema.columns "
            "WHERE table_schema = 'devbrain' AND table_name = 'memory'"
        )
        actual = {
            name: (data_type, is_nullable, udt_name)
            for name, data_type, is_nullable, udt_name in cur.fetchall()
        }

    missing = set(expected) - set(actual)
    assert not missing, f"missing columns: {missing}"
    for col, (data_type, nullable) in expected.items():
        assert actual[col][0] == data_type, (
            f"{col}: expected data_type={data_type!r}, got {actual[col][0]!r}"
        )
        assert actual[col][1] == nullable, (
            f"{col}: expected nullable={nullable!r}, got {actual[col][1]!r}"
        )
    assert actual["embedding"][2] == "vector", (
        f"embedding udt_name should be 'vector', got {actual['embedding'][2]!r}"
    )


# ─── 3. CHECK constraint on kind ─────────────────────────────────────────────


def test_memory_kind_check_constraint(db):
    pid = _devbrain_project_id(db)

    # Bad kind raises CheckViolation.
    with pytest.raises(psycopg2.errors.CheckViolation):
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO devbrain.memory (project_id, kind, content) "
                "VALUES (%s, %s, %s)",
                (pid, "not_real", f"{TEST_CONTENT_PREFIX}bad_kind"),
            )
            conn.commit()

    # All 5 valid kinds insert.
    for kind in ("chunk", "decision", "pattern", "issue", "session_summary"):
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO devbrain.memory (project_id, kind, content) "
                "VALUES (%s, %s, %s)",
                (pid, kind, f"{TEST_CONTENT_PREFIX}kind_{kind}"),
            )
            conn.commit()


# ─── 4. CHECK constraint on tier ─────────────────────────────────────────────


def test_memory_tier_check_constraint(db):
    pid = _devbrain_project_id(db)

    with pytest.raises(psycopg2.errors.CheckViolation):
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO devbrain.memory "
                "(project_id, kind, content, tier) VALUES (%s, %s, %s, %s)",
                (pid, "decision", f"{TEST_CONTENT_PREFIX}bad_tier", "admin"),
            )
            conn.commit()

    for tier in ("memory", "lesson", "rule"):
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO devbrain.memory "
                "(project_id, kind, content, tier) VALUES (%s, %s, %s, %s)",
                (pid, "decision", f"{TEST_CONTENT_PREFIX}tier_{tier}", tier),
            )
            conn.commit()


# ─── 5. project_id FK enforced ───────────────────────────────────────────────


def test_memory_project_fk_enforced(db):
    bogus = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO devbrain.memory (project_id, kind, content) "
                "VALUES (%s, %s, %s)",
                (bogus, "decision", f"{TEST_CONTENT_PREFIX}bad_fk"),
            )
            conn.commit()


# ─── 6. Defaults populate as declared ────────────────────────────────────────


def test_memory_defaults(db):
    pid = _devbrain_project_id(db)
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory (project_id, kind, content) "
            "VALUES (%s, %s, %s) RETURNING strength, hit_count, tier, "
            "archived_at, created_at, updated_at",
            (pid, "decision", f"{TEST_CONTENT_PREFIX}defaults"),
        )
        strength, hit_count, tier, archived_at, created_at, updated_at = (
            cur.fetchone()
        )
        conn.commit()

    assert strength == Decimal("1.0")
    assert hit_count == 0
    assert tier == "memory"
    assert archived_at is None
    assert created_at is not None
    assert updated_at is not None


# ─── 7. embedding dimension enforced ─────────────────────────────────────────


def test_memory_embedding_dim(db):
    pid = _devbrain_project_id(db)
    good = "[" + ",".join(["0.0"] * 1024) + "]"
    bad = "[" + ",".join(["0.0"] * 512) + "]"

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory "
            "(project_id, kind, content, embedding) "
            "VALUES (%s, %s, %s, %s::vector)",
            (pid, "chunk", f"{TEST_CONTENT_PREFIX}emb_good", good),
        )
        conn.commit()

    # pgvector raises a DataException on dimension mismatch; catch the
    # broad psycopg2.Error to avoid coupling to the precise subclass.
    with pytest.raises(psycopg2.Error):
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO devbrain.memory "
                "(project_id, kind, content, embedding) "
                "VALUES (%s, %s, %s, %s::vector)",
                (pid, "chunk", f"{TEST_CONTENT_PREFIX}emb_bad", bad),
            )
            conn.commit()


# ─── 8. All 5 expected indexes present ───────────────────────────────────────


def test_memory_indexes_exist(db):
    expected = {
        "idx_memory_project_kind",
        "idx_memory_embedding",
        "idx_memory_strength",
        "idx_memory_applies_when",
        "idx_memory_provenance",
    }
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'devbrain' AND tablename = 'memory'"
        )
        present = {r[0] for r in cur.fetchall()}
    missing = expected - present
    assert not missing, f"missing indexes: {missing}"


# ─── 9. Migration recorded in schema_migrations ──────────────────────────────


def test_010_recorded_in_schema_migrations(db):
    """Integration check: by the time tests run, `bin/devbrain migrate`
    (or the same code path called from setup) has applied 010 and the
    runner has recorded the row. If 010 ran but wasn't recorded the
    runner would re-apply it on every install — surface that bug here."""
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM devbrain.schema_migrations "
            "WHERE filename = '010_unified_memory.sql'"
        )
        assert cur.fetchone() is not None, (
            "010_unified_memory.sql is not recorded in "
            "devbrain.schema_migrations — check that the runner ran "
            "and that the filename is exact"
        )
