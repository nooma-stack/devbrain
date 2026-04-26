"""Backfill historical legacy rows into devbrain.memory (P2.c).

Run AFTER P2.b dual-writes are deployed: any rows landing in legacy
tables from then on are dual-written, so the partial unique index
from migration 011 (idx_memory_provenance_kind_unique) collapses
backfill + dual-write races to a single memory row.

Strategy
--------
* **Per-table backfills** — `backfill_chunks`, `backfill_decisions`,
  `backfill_patterns`, `backfill_issues`, `backfill_raw_sessions`. Plus
  `backfill_all` to run the lot in deterministic order.
* **Keyset pagination** (`WHERE id > %s ORDER BY id LIMIT N`) — UUID
  ordering is stable and sidesteps OFFSET cost on the chunks table
  (~10k+ rows already in production instances).
* **Per-batch transactions, best-effort recovery** — on batch failure
  log + rollback that batch, increment `batch_failures`, advance
  `last_id` to the batch's max id and continue. Operators re-run to
  mop up.
* **Embedding reuse** — `SELECT embedding::text` returns the pgvector
  literal as a Python string ("[v1,v2,…]") which we pass through to
  the INSERT as `%s::vector`. Never call Ollama; the legacy row paid
  that cost already.
* **Historical timestamp preservation** — the INSERT sets
  `created_at = legacy.created_at` so memory rows reflect when the
  data was originally captured. `updated_at` keeps memory's default
  (`now()`) since the row is "new" from memory's POV.
* **Idempotency** — same ON CONFLICT DO NOTHING shape as
  `ingest/memory_writer.py:record_memory`; relies on migration 011's
  partial unique index on `(provenance_id, kind) WHERE provenance_id
  IS NOT NULL`. Re-running is safe.
* **Schema-existence guard at entry point** — `_ensure_schema` checks
  the memory table and the unique index exist before any write,
  raising a clear "run `bin/devbrain migrate` first" error if not.
  Past-review lesson: surface schema drift loudly at the migration
  entry point rather than buried in mid-batch errors.

Mapping (legacy → memory)
-------------------------
+--------------+-------------------+------------------+--------------+-----------------+
| legacy table | kind              | content          | title        | embedding       |
+--------------+-------------------+------------------+--------------+-----------------+
| chunks       | 'chunk'           | content          | NULL         | embedding::text |
| decisions    | 'decision'        | decision         | title        | NULL            |
| patterns     | 'pattern'         | description      | name[:80]    | NULL            |
| issues       | 'issue'           | description      | title        | NULL            |
| raw_sessions | 'session_summary' | summary (req'd)  | summary[:80] | NULL            |
+--------------+-------------------+------------------+--------------+-----------------+

All five tables: skip rows with `project_id IS NULL`
(`devbrain.memory.project_id` is NOT NULL). raw_sessions also skips
rows where `summary IS NULL` — falling back to `raw_content` would
dump multi-megabyte transcripts into `memory.content`.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


_INSERT_MEMORY_SQL = """
    INSERT INTO devbrain.memory
        (project_id, kind, title, content, embedding, provenance_id, created_at)
    VALUES (%s, %s, %s, %s, %s::vector, %s, %s)
    ON CONFLICT (provenance_id, kind) WHERE provenance_id IS NOT NULL
    DO NOTHING
"""

# Sentinel UUID smaller than any real UUID — keyset pagination starts here.
_ZERO_UUID = "00000000-0000-0000-0000-000000000000"


def _new_counts() -> dict:
    """Empty counters dict in the canonical shape returned by every
    backfill function (so callers can sum across kinds without KeyError)."""
    return {
        "scanned": 0,
        "inserted": 0,
        "skipped_dup": 0,
        "skipped_no_project": 0,
        "skipped_no_summary": 0,
        "batch_failures": 0,
        "duration_s": 0.0,
    }


def _ensure_schema(db) -> None:
    """Verify devbrain.memory + idx_memory_provenance_kind_unique exist.

    Raises RuntimeError with a hint to run `bin/devbrain migrate` if
    either is missing. Called once from `backfill_all` and from each
    per-table entry point so direct callers (tests, scripts) get the
    same protection.
    """
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'devbrain' AND table_name = 'memory'"
        )
        if cur.fetchone() is None:
            raise RuntimeError(
                "devbrain.memory table is missing — "
                "run `bin/devbrain migrate` first (P2.a / migration 010)."
            )
        cur.execute(
            "SELECT 1 FROM pg_indexes "
            "WHERE schemaname = 'devbrain' AND tablename = 'memory' "
            "AND indexname = 'idx_memory_provenance_kind_unique'"
        )
        if cur.fetchone() is None:
            raise RuntimeError(
                "idx_memory_provenance_kind_unique index is missing — "
                "run `bin/devbrain migrate` first (P2.b / migration 011); "
                "without it the ON CONFLICT clause cannot infer a constraint."
            )


def _dry_run_counts(
    db,
    *,
    table: str,
    kind: str,
    extra_skip_predicate: str = "",
) -> dict:
    """Read-only counts that approximate what a real backfill would do.

    Three SELECTs against the legacy table + an anti-join against
    `devbrain.memory` for the would-insert tally. `skipped_dup` is the
    residual: rows we'd scan that already have a matching memory row
    (typical after P2.b dual-write has been live for a while).

    Args:
        db: FactoryDB.
        table: legacy table short name (e.g., 'chunks', 'raw_sessions').
        kind: memory kind to anti-join on.
        extra_skip_predicate: optional extra WHERE clause for the
            "rows we would skip" branch (raw_sessions uses this for
            the summary-NULL guard).
    """
    counts = _new_counts()
    fq_table = f"devbrain.{table}"

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {fq_table}")
        counts["scanned"] = int(cur.fetchone()[0])

        cur.execute(
            f"SELECT count(*) FROM {fq_table} WHERE project_id IS NULL"
        )
        counts["skipped_no_project"] = int(cur.fetchone()[0])

        if extra_skip_predicate:
            cur.execute(
                f"SELECT count(*) FROM {fq_table} "
                f"WHERE project_id IS NOT NULL AND ({extra_skip_predicate})"
            )
            counts["skipped_no_summary"] = int(cur.fetchone()[0])

        anti_join_extra = (
            f" AND NOT ({extra_skip_predicate})" if extra_skip_predicate else ""
        )
        cur.execute(
            f"""
            SELECT count(*) FROM {fq_table} t
            WHERE t.project_id IS NOT NULL{anti_join_extra}
              AND NOT EXISTS (
                  SELECT 1 FROM devbrain.memory m
                  WHERE m.provenance_id = t.id AND m.kind = %s
              )
            """,
            (kind,),
        )
        would_insert = int(cur.fetchone()[0])
        counts["inserted"] = would_insert

        counts["skipped_dup"] = (
            counts["scanned"]
            - counts["skipped_no_project"]
            - counts["skipped_no_summary"]
            - would_insert
        )
        if counts["skipped_dup"] < 0:
            # Defensive: counts can race against concurrent dual-writes
            # adding new rows between SELECTs. Clamp to 0 so dry-run
            # output is never confusingly negative.
            counts["skipped_dup"] = 0
    return counts


def _run_batched_backfill(
    db,
    *,
    select_sql: str,
    row_to_insert_args,
    batch_size: int,
    skip_filters,
) -> dict:
    """Generic keyset-paged loop shared by all per-table backfills.

    Args:
        db: FactoryDB.
        select_sql: SELECT with placeholders for `(last_id, batch_size)`
            and ordered `id ASC`. Must return rows whose first column is
            the legacy id (used to advance `last_id`).
        row_to_insert_args: callable(row, counts) -> tuple|None.
            Returns the args tuple for `_INSERT_MEMORY_SQL` (7 values),
            or None to skip this row (e.g., NULL project / NULL summary).
            Also responsible for incrementing skip counters in `counts`.
        batch_size: rows per batch.
        skip_filters: Iterable of human-readable filter names (e.g.
            ['project_id IS NULL']) — currently informational only,
            used in trace logging.
    """
    counts = _new_counts()
    started = time.perf_counter()
    last_id = _ZERO_UUID
    _ = list(skip_filters)  # touch arg so callers must pass it explicitly

    while True:
        # Fetch one batch in its own short transaction. If the SELECT
        # itself fails (rare — connection blip), record one batch
        # failure and break: we have no last_id to advance to.
        try:
            with db._conn() as conn, conn.cursor() as cur:
                cur.execute(select_sql, (last_id, batch_size))
                rows = cur.fetchall()
        except Exception as exc:
            logger.warning(
                "backfill SELECT failed (last_id=%s): %s", last_id, exc
            )
            counts["batch_failures"] += 1
            break

        if not rows:
            break

        # Build INSERT args for the rows we're not skipping. We do the
        # mapping outside the write transaction so a buggy mapper
        # function can't silently roll back a healthy batch.
        batch_args = []
        for row in rows:
            counts["scanned"] += 1
            args = row_to_insert_args(row, counts)
            if args is not None:
                batch_args.append(args)

        # Always advance last_id even when this batch had no inserts
        # (every row could have been skipped or dup) — otherwise we'd
        # loop forever on a page of skips.
        last_id = str(rows[-1][0])

        if not batch_args:
            if len(rows) < batch_size:
                break
            continue

        # One transaction per batch of inserts. ON CONFLICT DO NOTHING
        # silently dedupes against existing memory rows (dual-writes
        # racing the backfill, or earlier backfill runs). We use
        # `cur.rowcount` after `executemany` to count actual inserts;
        # PostgreSQL reports the number of rows affected, which equals
        # batch_args length minus the conflict skips.
        try:
            with db._conn() as conn, conn.cursor() as cur:
                # executemany hides per-row rowcount on some drivers;
                # iterate so we can tally inserted vs. dup precisely.
                inserted_this_batch = 0
                for args in batch_args:
                    cur.execute(_INSERT_MEMORY_SQL, args)
                    if cur.rowcount == 1:
                        inserted_this_batch += 1
                conn.commit()
                counts["inserted"] += inserted_this_batch
                counts["skipped_dup"] += (
                    len(batch_args) - inserted_this_batch
                )
        except Exception as exc:
            logger.warning(
                "backfill batch failed (last_id=%s, size=%d): %s",
                last_id, len(batch_args), exc,
            )
            counts["batch_failures"] += 1
            # last_id already advanced above; next loop picks up after
            # this batch. ON CONFLICT covers any rows that did commit
            # before the failure on a re-run.

        if len(rows) < batch_size:
            # Final partial page — nothing more to fetch.
            break

    counts["duration_s"] = round(time.perf_counter() - started, 3)
    return counts


# ─── Per-table backfills ─────────────────────────────────────────────────────


def backfill_chunks(
    db,
    *,
    batch_size: int = 1000,
    dry_run: bool = False,
) -> dict:
    """Backfill kind='chunk' from devbrain.chunks.

    Reuses the legacy embedding via `embedding::text` → `%s::vector`.
    Skips rows with project_id IS NULL.
    """
    _ensure_schema(db)
    if dry_run:
        return _dry_run_counts(db, table="chunks", kind="chunk")

    select_sql = """
        SELECT id, project_id, content, embedding::text, created_at
        FROM devbrain.chunks
        WHERE id > %s
        ORDER BY id
        LIMIT %s
    """

    def to_args(row, counts):
        chunk_id, project_id, content, embedding_text, created_at = row
        if project_id is None:
            counts["skipped_no_project"] += 1
            return None
        return (
            str(project_id), "chunk", None, content, embedding_text,
            str(chunk_id), created_at,
        )

    return _run_batched_backfill(
        db,
        select_sql=select_sql,
        row_to_insert_args=to_args,
        batch_size=batch_size,
        skip_filters=["project_id IS NULL"],
    )


def backfill_decisions(
    db,
    *,
    batch_size: int = 1000,
    dry_run: bool = False,
) -> dict:
    """Backfill kind='decision' from devbrain.decisions.

    title is the existing 500-char column; content is the `decision`
    text (the actual decision body). No embedding column on this
    table.
    """
    _ensure_schema(db)
    if dry_run:
        return _dry_run_counts(db, table="decisions", kind="decision")

    select_sql = """
        SELECT id, project_id, title, decision, created_at
        FROM devbrain.decisions
        WHERE id > %s
        ORDER BY id
        LIMIT %s
    """

    def to_args(row, counts):
        decision_id, project_id, title, decision_text, created_at = row
        if project_id is None:
            counts["skipped_no_project"] += 1
            return None
        return (
            str(project_id), "decision", title, decision_text, None,
            str(decision_id), created_at,
        )

    return _run_batched_backfill(
        db,
        select_sql=select_sql,
        row_to_insert_args=to_args,
        batch_size=batch_size,
        skip_filters=["project_id IS NULL"],
    )


def backfill_patterns(
    db,
    *,
    batch_size: int = 1000,
    dry_run: bool = False,
) -> dict:
    """Backfill kind='pattern' from devbrain.patterns.

    title = name[:80] (legacy `name` is varchar(255)); content =
    description.
    """
    _ensure_schema(db)
    if dry_run:
        return _dry_run_counts(db, table="patterns", kind="pattern")

    select_sql = """
        SELECT id, project_id, name, description, created_at
        FROM devbrain.patterns
        WHERE id > %s
        ORDER BY id
        LIMIT %s
    """

    def to_args(row, counts):
        pattern_id, project_id, name, description, created_at = row
        if project_id is None:
            counts["skipped_no_project"] += 1
            return None
        title = (name or "")[:80] or None
        return (
            str(project_id), "pattern", title, description, None,
            str(pattern_id), created_at,
        )

    return _run_batched_backfill(
        db,
        select_sql=select_sql,
        row_to_insert_args=to_args,
        batch_size=batch_size,
        skip_filters=["project_id IS NULL"],
    )


def backfill_issues(
    db,
    *,
    batch_size: int = 1000,
    dry_run: bool = False,
) -> dict:
    """Backfill kind='issue' from devbrain.issues.

    title is the existing 500-char column; content is `description`.
    """
    _ensure_schema(db)
    if dry_run:
        return _dry_run_counts(db, table="issues", kind="issue")

    select_sql = """
        SELECT id, project_id, title, description, created_at
        FROM devbrain.issues
        WHERE id > %s
        ORDER BY id
        LIMIT %s
    """

    def to_args(row, counts):
        issue_id, project_id, title, description, created_at = row
        if project_id is None:
            counts["skipped_no_project"] += 1
            return None
        return (
            str(project_id), "issue", title, description, None,
            str(issue_id), created_at,
        )

    return _run_batched_backfill(
        db,
        select_sql=select_sql,
        row_to_insert_args=to_args,
        batch_size=batch_size,
        skip_filters=["project_id IS NULL"],
    )


def backfill_raw_sessions(
    db,
    *,
    batch_size: int = 1000,
    dry_run: bool = False,
) -> dict:
    """Backfill kind='session_summary' from devbrain.raw_sessions.

    Skips rows with summary IS NULL (or empty) — falling back to
    raw_content would dump multi-megabyte transcripts into
    memory.content. Operators run the summarizer first if they want
    those rows covered.
    """
    _ensure_schema(db)
    if dry_run:
        return _dry_run_counts(
            db,
            table="raw_sessions",
            kind="session_summary",
            extra_skip_predicate="summary IS NULL OR summary = ''",
        )

    select_sql = """
        SELECT id, project_id, summary, created_at
        FROM devbrain.raw_sessions
        WHERE id > %s
        ORDER BY id
        LIMIT %s
    """

    def to_args(row, counts):
        session_id, project_id, summary, created_at = row
        if project_id is None:
            counts["skipped_no_project"] += 1
            return None
        if not summary:
            counts["skipped_no_summary"] += 1
            return None
        title = summary[:80]
        return (
            str(project_id), "session_summary", title, summary, None,
            str(session_id), created_at,
        )

    return _run_batched_backfill(
        db,
        select_sql=select_sql,
        row_to_insert_args=to_args,
        batch_size=batch_size,
        skip_filters=["project_id IS NULL", "summary IS NULL"],
    )


# ─── Aggregator ──────────────────────────────────────────────────────────────


_BACKFILLS = (
    ("chunks", backfill_chunks),
    ("decisions", backfill_decisions),
    ("patterns", backfill_patterns),
    ("issues", backfill_issues),
    ("raw_sessions", backfill_raw_sessions),
)


def backfill_all(
    db,
    *,
    batch_size: int = 1000,
    dry_run: bool = False,
) -> dict:
    """Run all five per-table backfills in deterministic order.

    Returns a dict keyed by legacy-table short name plus a "TOTAL"
    aggregate. Each entry is the counts dict from the corresponding
    `backfill_*` call.
    """
    _ensure_schema(db)
    results: dict = {}
    total = _new_counts()
    for label, fn in _BACKFILLS:
        counts = fn(db, batch_size=batch_size, dry_run=dry_run)
        results[label] = counts
        for key in total:
            total[key] += counts.get(key, 0)
    # duration_s should be a sum of the per-table durations rather than
    # an integer-rounded sum of float roundings; recompute once.
    total["duration_s"] = round(total["duration_s"], 3)
    results["TOTAL"] = total
    return results
