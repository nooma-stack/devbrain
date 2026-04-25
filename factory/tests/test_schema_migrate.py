"""Tests for the migration applier (schema_migrate.py).

Uses the live devbrain DB (same convention as the rest of this directory's
integration tests). Every row inserted under a `schema_migrate_test_*`
filename is cleaned up by the autouse fixture so the schema_migrations
table doesn't accumulate test leftovers.
"""
from __future__ import annotations

import logging
from pathlib import Path

import psycopg2
import psycopg2.errors
import pytest

import schema_migrate
from config import DATABASE_URL
from state_machine import FactoryDB

# All test-created filenames start with this prefix so the cleanup
# fixture can wipe them with a single LIKE query.
TEST_FILENAME_PREFIX = "schema_migrate_test_"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture(autouse=True)
def _cleanup(db):
    yield
    # Wrap in try/rollback because some tests deliberately stub out the
    # tracking table to simulate the pre-migration state.
    try:
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM devbrain.schema_migrations WHERE filename LIKE %s",
                (f"{TEST_FILENAME_PREFIX}%",),
            )
            cur.execute("DROP TABLE IF EXISTS devbrain.schema_migrate_test_tmp")
            cur.execute("DROP TABLE IF EXISTS devbrain.schema_migrate_test_lock_tmp")
            conn.commit()
    except psycopg2.errors.UndefinedTable:
        pass


def _write(dir_: Path, name: str, body: str) -> Path:
    full = dir_ / f"{TEST_FILENAME_PREFIX}{name}"
    full.write_text(body)
    return full


def _filename_recorded(db, filename: str) -> bool:
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM devbrain.schema_migrations WHERE filename = %s",
            (filename,),
        )
        return cur.fetchone() is not None


def _table_exists(db, table: str) -> bool:
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass(%s) IS NOT NULL", (f"devbrain.{table}",),
        )
        return cur.fetchone()[0]


# ─── 1. list_pending — tracking table missing ────────────────────────────────


def test_list_pending_when_tracking_table_missing(db, tmp_path, monkeypatch):
    """If schema_migrations doesn't exist, every file on disk is pending."""
    a = _write(tmp_path, "a.sql", "SELECT 1;")
    b = _write(tmp_path, "b.sql", "SELECT 2;")

    real_conn = db._conn

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False

        def execute(self, *_a, **_kw):
            raise psycopg2.errors.UndefinedTable(
                "relation \"devbrain.schema_migrations\" does not exist"
            )

        def fetchall(self): return []
        def close(self): pass

    class _FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()
        def close(self): pass

    monkeypatch.setattr(db, "_conn", lambda: _FakeConn())

    pending = schema_migrate.list_pending(db, tmp_path)
    assert pending == [a, b]

    # Restore the real _conn so the cleanup fixture can run.
    monkeypatch.setattr(db, "_conn", real_conn)


# ─── 2. list_pending — only unapplied returned ───────────────────────────────


def test_list_pending_returns_only_unapplied(db, tmp_path):
    a = _write(tmp_path, "a.sql", "SELECT 1;")
    b = _write(tmp_path, "b.sql", "SELECT 2;")
    c = _write(tmp_path, "c.sql", "SELECT 3;")

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.schema_migrations (filename) VALUES (%s), (%s)",
            (a.name, b.name),
        )
        conn.commit()

    pending = schema_migrate.list_pending(db, tmp_path)
    assert pending == [c]


# ─── 3. apply_one — records row on success ───────────────────────────────────


def test_apply_one_records_row_on_success(db, tmp_path):
    sql = _write(
        tmp_path, "create_tmp.sql",
        "CREATE TABLE devbrain.schema_migrate_test_tmp (id INT);",
    )

    schema_migrate.apply_one(db, sql)

    assert _filename_recorded(db, sql.name)
    assert _table_exists(db, "schema_migrate_test_tmp")


# ─── 4. apply_one — rolls back on failure ────────────────────────────────────


def test_apply_one_rolls_back_on_failure(db, tmp_path):
    sql = _write(
        tmp_path, "broken.sql",
        # Valid first statement followed by a syntax error — both must
        # roll back together with the tracking insert.
        "CREATE TABLE devbrain.schema_migrate_test_tmp (id INT);\n"
        "NOT VALID SQL;",
    )

    with pytest.raises(psycopg2.Error):
        schema_migrate.apply_one(db, sql)

    assert not _filename_recorded(db, sql.name)
    assert not _table_exists(db, "schema_migrate_test_tmp")


# ─── 5. migrate — applies in lexical order ───────────────────────────────────


def test_migrate_applies_in_lexical_order(db, tmp_path):
    # Write out of order — the runner must still apply a, then b, then c.
    _write(tmp_path, "c.sql", "SELECT 3;")
    _write(tmp_path, "a.sql", "SELECT 1;")
    _write(tmp_path, "b.sql", "SELECT 2;")

    applied = schema_migrate.migrate(db, migrations_dir=tmp_path)
    assert applied == [
        f"{TEST_FILENAME_PREFIX}a.sql",
        f"{TEST_FILENAME_PREFIX}b.sql",
        f"{TEST_FILENAME_PREFIX}c.sql",
    ]


# ─── 6. migrate — dry-run does not apply ─────────────────────────────────────


def test_migrate_dry_run_does_not_apply(db, tmp_path):
    sql = _write(
        tmp_path, "tmp.sql",
        "CREATE TABLE devbrain.schema_migrate_test_tmp (id INT);",
    )

    result = schema_migrate.migrate(db, migrations_dir=tmp_path, dry_run=True)
    assert result == [sql.name]
    assert not _filename_recorded(db, sql.name)
    assert not _table_exists(db, "schema_migrate_test_tmp")


# ─── 7. concurrent migrate — second caller skips ─────────────────────────────


def test_concurrent_migrate_second_caller_skips(db, tmp_path):
    """If another process holds the advisory lock, migrate() returns []."""
    sql = _write(
        tmp_path, "blocked.sql",
        "CREATE TABLE devbrain.schema_migrate_test_lock_tmp (id INT);",
    )

    # Take the advisory lock from a separate connection — the migrate()
    # call below should see pg_try_advisory_lock return false and bail.
    holder = psycopg2.connect(DATABASE_URL)
    try:
        with holder.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_lock(%s)", (schema_migrate._LOCK_KEY,),
            )
        holder.commit()

        result = schema_migrate.migrate(db, migrations_dir=tmp_path)
        assert result == []
        assert not _filename_recorded(db, sql.name)
        assert not _table_exists(db, "schema_migrate_test_lock_tmp")
    finally:
        with holder.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_unlock(%s)", (schema_migrate._LOCK_KEY,),
            )
        holder.commit()
        holder.close()


# ─── 8. migrate — file deleted from disk logs warning ────────────────────────


def test_migrate_after_file_deleted_logs_warning(db, tmp_path, caplog):
    """A row in schema_migrations whose file is gone produces a warning,
    not a spurious pending entry."""
    ghost_filename = f"{TEST_FILENAME_PREFIX}ghost.sql"
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.schema_migrations (filename) VALUES (%s)",
            (ghost_filename,),
        )
        conn.commit()

    with caplog.at_level(logging.WARNING, logger="schema_migrate"):
        result = schema_migrate.migrate(db, migrations_dir=tmp_path, dry_run=True)

    assert result == []
    assert any(
        "no longer on disk" in rec.message and ghost_filename in rec.message
        for rec in caplog.records
    )


# ─── 9. migrate — end-to-end against a DB with no tracking table ─────────────


def test_migrate_end_to_end_when_tracking_table_missing(db):
    """Simulates the pre-009 upgrade path: drop devbrain.schema_migrations,
    run migrate() against the real migrations/ directory, and verify the
    table is rebuilt with 001-009 backfilled — without re-running 001
    (which would error on the already-existing devbrain.projects table
    since most of its CREATE TABLE statements lack IF NOT EXISTS).
    """
    from config import DEVBRAIN_HOME

    real_migrations = DEVBRAIN_HOME / "migrations"
    assert (real_migrations / "009_schema_migrations.sql").exists(), (
        "test prerequisite: 009_schema_migrations.sql must exist on disk"
    )

    # Snapshot the state we'll restore on failure.
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS devbrain.schema_migrations")
        conn.commit()
        # Sanity check: table is gone, but devbrain.projects (from 001)
        # still exists — that's the upgrade scenario.
        cur.execute("SELECT to_regclass('devbrain.schema_migrations')")
        assert cur.fetchone()[0] is None
        cur.execute("SELECT to_regclass('devbrain.projects')")
        assert cur.fetchone()[0] is not None

    try:
        applied = schema_migrate.migrate(db, migrations_dir=real_migrations)

        # 009 must be in the applied list — it's the bootstrap file the
        # runner ran. Anything ≥ 010 on disk also gets applied; 001-008
        # do NOT (the bootstrap INSERT marks them).
        assert "009_schema_migrations.sql" in applied
        for old in (
            "001_initial_schema.sql",
            "002_create_vector_indexes.sql",
            "003_cleanup_agent.sql",
            "004_file_registry.sql",
            "005_notifications.sql",
            "006_blocked_state.sql",
            "007_factory_runtime_state.sql",
            "008_artifact_warning_count.sql",
        ):
            assert old not in applied, (
                f"{old} should NOT be re-applied on upgrade — its CREATE "
                f"TABLE statements would error on the existing schema"
            )

        # Tracking table is rebuilt and backfilled.
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('devbrain.schema_migrations')")
            assert cur.fetchone()[0] is not None
            cur.execute(
                "SELECT filename FROM devbrain.schema_migrations "
                "WHERE filename LIKE '00%_%.sql' ORDER BY filename"
            )
            recorded = [r[0] for r in cur.fetchall()]
        for expected in (
            "001_initial_schema.sql",
            "002_create_vector_indexes.sql",
            "003_cleanup_agent.sql",
            "004_file_registry.sql",
            "005_notifications.sql",
            "006_blocked_state.sql",
            "007_factory_runtime_state.sql",
            "008_artifact_warning_count.sql",
            "009_schema_migrations.sql",
        ):
            assert expected in recorded, f"{expected} not in {recorded}"

        # The pre-existing schema is intact (i.e., 001 wasn't re-run).
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('devbrain.projects')")
            assert cur.fetchone()[0] is not None
    except Exception:
        # If the test fails partway, ensure schema_migrations is restored
        # so subsequent tests aren't broken.
        with db._conn() as conn, conn.cursor() as cur:
            sql_text = (real_migrations / "009_schema_migrations.sql").read_text()
            cur.execute(sql_text)
            conn.commit()
        raise
