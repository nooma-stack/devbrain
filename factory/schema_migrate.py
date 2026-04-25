"""Apply pending schema migrations from the migrations/ directory.

The runner sorts `migrations/*.sql` lexically, looks each filename up in
`devbrain.schema_migrations`, and executes any that haven't already been
recorded — taking a process-wide Postgres advisory lock first so two
concurrent installers can't apply the same file twice.

Each file is applied in its own transaction along with the matching
INSERT into `schema_migrations`, so a mid-file failure rolls back both
the schema change and the tracking row.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import psycopg2
import psycopg2.errors

from config import DEVBRAIN_HOME

logger = logging.getLogger(__name__)

# Stable, repo-unique key for pg_advisory_lock. Picked deliberately to
# avoid collision with any other advisory-lock callers in DevBrain
# (currently none).
_LOCK_KEY = 4720250424


def list_pending(db, migrations_dir: Path) -> list[Path]:
    """Return the migrations on disk that are not yet recorded as applied.

    If the schema_migrations table itself doesn't exist yet (fresh install
    before 009 has run), every file on disk is pending.
    """
    all_files = sorted(migrations_dir.glob("*.sql"))
    try:
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT filename FROM devbrain.schema_migrations")
            applied = {r[0] for r in cur.fetchall()}
    except psycopg2.errors.UndefinedTable:
        return all_files
    on_disk = {f.name for f in all_files}
    for missing in sorted(applied - on_disk):
        logger.warning(
            "schema_migrations records %s but the file is no longer on disk",
            missing,
        )
    return [f for f in all_files if f.name not in applied]


def apply_one(db, path: Path) -> None:
    """Run one migration file and record it in schema_migrations.

    Both the SQL execution and the tracking INSERT happen in the same
    transaction so they commit (or roll back) together.
    """
    sql_text = path.read_text()
    start = time.perf_counter()
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(sql_text)
        cur.execute(
            "INSERT INTO devbrain.schema_migrations (filename) VALUES (%s) "
            "ON CONFLICT (filename) DO NOTHING",
            (path.name,),
        )
        conn.commit()
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    logger.info("[migrate] applied %s (%dms)", path.name, elapsed_ms)


def migrate(db, migrations_dir: Path | None = None, dry_run: bool = False) -> list[str]:
    """Apply all pending migrations and return their filenames in order.

    With dry_run=True, returns the list of pending filenames without
    applying anything (and without taking the advisory lock).

    If another process holds the advisory lock, returns [] without
    waiting — the other process will finish the work.
    """
    if migrations_dir is None:
        migrations_dir = DEVBRAIN_HOME / "migrations"

    pending = list_pending(db, migrations_dir)
    if dry_run:
        return [p.name for p in pending]
    if not pending:
        return []

    # Hold the advisory lock on a dedicated connection so it survives
    # across the per-file connections in apply_one(). Releasing the lock
    # is best-effort (closing the connection releases it anyway).
    lock_conn = db._conn()
    try:
        with lock_conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_KEY,))
            got_lock = cur.fetchone()[0]
        if not got_lock:
            logger.info(
                "[migrate] another devbrain migrate is in progress; skipping"
            )
            return []

        # Re-list under the lock — closes the tiny race window where two
        # callers both observed the same pending set just before one
        # grabbed the lock.
        pending = list_pending(db, migrations_dir)
        applied: list[str] = []
        for path in pending:
            apply_one(db, path)
            applied.append(path.name)
        return applied
    finally:
        try:
            with lock_conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
        finally:
            lock_conn.close()
