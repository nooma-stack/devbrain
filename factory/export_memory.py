"""Export DevBrain memory + raw sessions for cross-machine migration (#5.b).

Pairs with :mod:`factory.import_memory`. The two together let an operator
move accumulated memory between DevBrain instances (canonical use case:
MacBook → Mac Studio after a hardware swap) without touching Postgres
internals or pgvector binary dumps.

Scope
-----
The export covers the four tables that hold a project's accumulated
intelligence:

* ``devbrain.projects`` — the registry the rest of the export joins on
* ``devbrain.devs``     — local notification routing / channel config
* ``devbrain.memory``   — the unified memory table (P2.a+)
* ``devbrain.raw_sessions`` — the lossless transcripts memory points back to

Legacy tables (chunks/decisions/patterns/issues) are *not* exported:
P2.d.i has already switched reads to ``devbrain.memory``, and the P2.c
backfill is the canonical path for surfacing pre-P2.b legacy data into
``memory``. Exporting legacy alongside memory would risk duplicating
rows on the destination once it runs its own backfill.

Wire format
-----------
A single JSON document (optionally gzipped) with the shape::

    {
      "version": 1,
      "exported_at": "2026-04-27T18:30:00+00:00",
      "source": {
        "database_url": "postgresql://devbrain:***@localhost:5433/devbrain",
        "schema_migration_top": "011_memory_provenance_unique.sql"
      },
      "projects":     [ {...}, ... ],
      "devs":         [ {...}, ... ],
      "memory":       [ {...}, ... ],
      "raw_sessions": [ {...}, ... ]
    }

* ``version`` — wire format version. Bump on incompatible changes.
* ``schema_migration_top`` — the highest filename in
  ``devbrain.schema_migrations``. The importer rejects any export whose
  top differs from the destination's, since cross-version row shapes
  may not be compatible. Source DB without ``schema_migrations`` (very
  old install) is exported with the value ``None`` and the importer
  refuses to load it.
* Every ``memory`` and ``raw_sessions`` row carries an extra
  ``project_slug`` field. UUIDs differ between instances, so the
  importer uses the slug to remap ``project_id`` to the destination's
  row id (creating the project on the fly if it's not already there).
* Embeddings round-trip as the pgvector text literal
  ``"[v1,v2,…]"`` — bit-equal on re-import via ``%s::vector``.

Streaming
---------
``memory`` and ``raw_sessions`` can be large. Both are read with a
named (server-side) cursor so the Python process never holds more than
``itersize`` rows at once. Output is written to disk incrementally —
the writer emits the array opening, then one row per line, then the
closing — so a multi-GB export doesn't have to fit in memory either.
"""
from __future__ import annotations

import gzip
import json
import logging
import uuid as _uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import IO, Any, Iterable
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

# Bump when the on-disk shape changes in a way the importer can't read
# transparently. The importer rejects unknown versions outright.
EXPORT_VERSION = 1

# Server-side cursor page size. Small enough that one page fits in
# memory comfortably, large enough to amortize the round-trip cost.
_CURSOR_ITERSIZE = 500


# ─── helpers ────────────────────────────────────────────────────────────────


class _ExportEncoder(json.JSONEncoder):
    """Encoder that handles the non-JSON-native types we read from psycopg2.

    * ``UUID`` → string
    * ``datetime`` / ``date`` → ISO-8601 string
    * ``Decimal`` → float (memory.strength is NUMERIC; loss of precision
      below ~15 digits is acceptable for a strength score)
    * ``memoryview`` / ``bytes`` → hex (defensive — no current column
      ships binary, but any future blob column won't crash the export)
    """

    def default(self, o: Any) -> Any:
        if isinstance(o, _uuid.UUID):
            return str(o)
        if isinstance(o, datetime):
            return o.isoformat()
        if isinstance(o, date):
            return o.isoformat()
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, memoryview):
            return bytes(o).hex()
        if isinstance(o, bytes):
            return o.hex()
        return super().default(o)


def _redact_url(url: str) -> str:
    """Mask the password component of a libpq URL.

    ``postgresql://devbrain:secret@host:5433/db`` →
    ``postgresql://devbrain:***@host:5433/db``. Used only for the
    ``source.database_url`` breadcrumb in the export header — never for
    actual connections.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "***"
    if parsed.password is None:
        return url
    user = parsed.username or ""
    host = parsed.hostname or ""
    netloc = f"{user}:***@{host}"
    if parsed.port is not None:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def _ensure_schema(db) -> None:
    """Verify devbrain.memory exists before opening any cursors.

    The importer's symmetric check is the strict one (it bails on any
    schema mismatch); the exporter's check is just a friendly early
    error so an operator running this on a fresh / un-migrated DB sees
    "run migrate first" instead of "relation does not exist".
    """
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'devbrain' AND table_name = 'memory'"
        )
        if cur.fetchone() is None:
            raise RuntimeError(
                "devbrain.memory table is missing — "
                "run `bin/devbrain migrate` first."
            )


def _highest_migration(db) -> str | None:
    """Return the lexically-greatest filename in ``schema_migrations``.

    The importer compares this against its own destination value to
    refuse cross-version migrations. ``None`` if the tracking table
    doesn't exist (pre-009 install) — the importer treats that as a
    hard failure too so an unknown source can't poison the destination.
    """
    with db._conn() as conn, conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT filename FROM devbrain.schema_migrations "
                "ORDER BY filename DESC LIMIT 1"
            )
        except Exception:
            return None
        row = cur.fetchone()
        return row[0] if row else None


def _resolve_slug_filter(db, slugs: Iterable[str]) -> dict[str, str]:
    """Map slug → project_id for the requested filter slugs.

    Slugs that don't exist in the source DB raise ``ValueError`` — fail
    loud rather than silently producing a partial export.
    """
    slugs = list(dict.fromkeys(slugs))  # preserve order, drop dupes
    if not slugs:
        return {}
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT slug, id FROM devbrain.projects WHERE slug = ANY(%s)",
            (slugs,),
        )
        found = {slug: str(pid) for slug, pid in cur.fetchall()}
    missing = [s for s in slugs if s not in found]
    if missing:
        raise ValueError(
            f"unknown project slug(s): {', '.join(missing)}"
        )
    return found


# ─── per-table fetchers ─────────────────────────────────────────────────────


def _fetch_projects(db, project_ids: list[str] | None) -> list[dict]:
    sql = (
        "SELECT id, slug, name, root_path, description, constraints, "
        "       tech_stack, lint_commands, test_commands, metadata, "
        "       created_at, updated_at "
        "FROM devbrain.projects "
    )
    params: tuple = ()
    if project_ids:
        # Stringified UUIDs need an explicit ::uuid[] cast — Postgres
        # won't auto-coerce text[] to uuid[] in the ANY operator.
        sql += "WHERE id = ANY(%s::uuid[]) "
        params = (project_ids,)
    sql += "ORDER BY slug"
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _fetch_devs(db) -> list[dict]:
    """Always exports all devs — they're not project-scoped."""
    with db._conn() as conn, conn.cursor() as cur:
        # The devs table only exists from migration 005 onward. Treat a
        # missing table as "no devs to export" rather than a hard error
        # — exports from instances that never ran 005 are still valid.
        try:
            cur.execute(
                "SELECT id, dev_id, full_name, channels, "
                "       event_subscriptions, created_at, updated_at "
                "FROM devbrain.devs ORDER BY dev_id"
            )
        except Exception:
            return []
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _stream_memory(db, project_ids: list[str] | None):
    """Yield memory rows one at a time via a server-side cursor.

    project_id is the source DB's id. We also yield ``project_slug`` so
    the importer can remap to the destination's project_id. Embedding
    is fetched as the pgvector text literal for lossless re-insertion.
    """
    sql = (
        "SELECT m.id, p.slug AS project_slug, m.kind, m.title, m.content, "
        "       m.embedding::text AS embedding_text, m.strength, m.hit_count, "
        "       m.last_hit, m.applies_when, m.provenance_id, m.tier, "
        "       m.archived_at, m.created_at, m.updated_at "
        "FROM devbrain.memory m "
        "JOIN devbrain.projects p ON p.id = m.project_id "
    )
    params: tuple = ()
    if project_ids:
        sql += "WHERE m.project_id = ANY(%s::uuid[]) "
        params = (project_ids,)
    sql += "ORDER BY m.created_at, m.id"

    # Named cursors are server-side; itersize controls the per-fetch
    # window. withhold=False means the cursor dies with the
    # transaction, which is exactly the lifetime we want here.
    with db._conn() as conn:
        cur = conn.cursor(name="export_memory_cursor")
        try:
            cur.itersize = _CURSOR_ITERSIZE
            cur.execute(sql, params)
            for row in cur:
                yield {
                    "id": row[0],
                    "project_slug": row[1],
                    "kind": row[2],
                    "title": row[3],
                    "content": row[4],
                    "embedding_text": row[5],
                    "strength": row[6],
                    "hit_count": row[7],
                    "last_hit": row[8],
                    "applies_when": row[9],
                    "provenance_id": row[10],
                    "tier": row[11],
                    "archived_at": row[12],
                    "created_at": row[13],
                    "updated_at": row[14],
                }
        finally:
            cur.close()


def _stream_raw_sessions(db, project_ids: list[str] | None):
    """Yield raw_sessions rows one at a time via a server-side cursor.

    Same shape as memory: include ``project_slug`` for cross-instance
    remap. project_id can be NULL (orphan sessions) — emit
    ``project_slug=None`` in that case so the importer can preserve
    the orphan or, if the operator opts in, attribute it later.
    """
    sql = (
        "SELECT r.id, p.slug AS project_slug, r.source_app, r.source_path, "
        "       r.source_hash, r.session_id, r.model_used, r.started_at, "
        "       r.ended_at, r.message_count, r.raw_content, r.summary, "
        "       r.files_touched, r.metadata, r.created_at "
        "FROM devbrain.raw_sessions r "
        "LEFT JOIN devbrain.projects p ON p.id = r.project_id "
    )
    params: tuple = ()
    if project_ids:
        # Inner-join semantics for the slug filter: orphan rows have no
        # slug, so they belong to no requested project.
        sql += "WHERE r.project_id = ANY(%s::uuid[]) "
        params = (project_ids,)
    sql += "ORDER BY r.created_at, r.id"

    with db._conn() as conn:
        cur = conn.cursor(name="export_raw_sessions_cursor")
        try:
            cur.itersize = _CURSOR_ITERSIZE
            cur.execute(sql, params)
            for row in cur:
                yield {
                    "id": row[0],
                    "project_slug": row[1],
                    "source_app": row[2],
                    "source_path": row[3],
                    "source_hash": row[4],
                    "session_id": row[5],
                    "model_used": row[6],
                    "started_at": row[7],
                    "ended_at": row[8],
                    "message_count": row[9],
                    "raw_content": row[10],
                    "summary": row[11],
                    "files_touched": row[12],
                    "metadata": row[13],
                    "created_at": row[14],
                }
        finally:
            cur.close()


# ─── public API ─────────────────────────────────────────────────────────────


def export_to_dict(
    db,
    *,
    project_slugs: Iterable[str] | None = None,
    database_url: str | None = None,
) -> dict:
    """Build the in-memory dict form of an export.

    Convenience wrapper for tests and small dumps. For real exports,
    prefer :func:`write_export_file` which streams through the cursor
    instead of materializing every row in Python.
    """
    _ensure_schema(db)

    project_ids: list[str] | None = None
    if project_slugs:
        slug_to_id = _resolve_slug_filter(db, project_slugs)
        project_ids = list(slug_to_id.values())

    raw = {
        "version": EXPORT_VERSION,
        "exported_at": datetime.now().astimezone().isoformat(),
        "source": {
            "database_url": (
                _redact_url(database_url) if database_url else None
            ),
            "schema_migration_top": _highest_migration(db),
        },
        "projects": _fetch_projects(db, project_ids),
        "devs": _fetch_devs(db),
        "memory": list(_stream_memory(db, project_ids)),
        "raw_sessions": list(_stream_raw_sessions(db, project_ids)),
    }
    # Round-trip through the JSON encoder so the in-memory form has the
    # same types the importer would see from a disk read (str UUIDs,
    # ISO-8601 datetimes). Otherwise tests / callers would have to know
    # which path they took to compare values consistently.
    return json.loads(json.dumps(raw, cls=_ExportEncoder))


def write_export_file(
    db,
    out_path: Path | str,
    *,
    project_slugs: Iterable[str] | None = None,
    database_url: str | None = None,
    gzip_output: bool | None = None,
) -> dict:
    """Stream an export to ``out_path``. Returns a counts summary.

    Args:
        db: FactoryDB instance.
        out_path: destination file. Parent directory must exist.
        project_slugs: optional whitelist; export everything when None.
        database_url: source URL for the redacted breadcrumb (pulled
            from the FactoryDB if not supplied).
        gzip_output: force gzip on/off. When None, infer from the
            ``.gz`` suffix on ``out_path``.

    Returns counts of rows written per table — useful for the CLI
    summary and the round-trip test.
    """
    _ensure_schema(db)

    out_path = Path(out_path)
    if gzip_output is None:
        gzip_output = out_path.suffix == ".gz"

    project_ids: list[str] | None = None
    if project_slugs:
        slug_to_id = _resolve_slug_filter(db, project_slugs)
        project_ids = list(slug_to_id.values())

    counts = {"projects": 0, "devs": 0, "memory": 0, "raw_sessions": 0}

    if database_url is None:
        database_url = getattr(db, "database_url", None)

    header = {
        "version": EXPORT_VERSION,
        "exported_at": datetime.now().astimezone().isoformat(),
        "source": {
            "database_url": (
                _redact_url(database_url) if database_url else None
            ),
            "schema_migration_top": _highest_migration(db),
        },
    }

    fh: IO[str]
    if gzip_output:
        fh = gzip.open(out_path, "wt", encoding="utf-8")  # type: ignore[assignment]
    else:
        fh = open(out_path, "w", encoding="utf-8")
    try:
        # Write the header keys, then stream each array. We do this by
        # hand instead of json.dump so the per-row stream never has to
        # be a generator-of-rows held in memory at once.
        fh.write("{\n")
        fh.write(f"  \"version\": {json.dumps(header['version'])},\n")
        fh.write(
            f"  \"exported_at\": {json.dumps(header['exported_at'])},\n"
        )
        fh.write(
            "  \"source\": "
            f"{json.dumps(header['source'], cls=_ExportEncoder)},\n"
        )

        # Small tables: render in one shot.
        projects = _fetch_projects(db, project_ids)
        counts["projects"] = len(projects)
        fh.write(
            "  \"projects\": "
            f"{json.dumps(projects, cls=_ExportEncoder)},\n"
        )

        devs = _fetch_devs(db)
        counts["devs"] = len(devs)
        fh.write(
            "  \"devs\": "
            f"{json.dumps(devs, cls=_ExportEncoder)},\n"
        )

        # Streamed tables: one row per line so a half-written file is
        # still mostly recoverable.
        for label, stream in (
            ("memory", _stream_memory(db, project_ids)),
            ("raw_sessions", _stream_raw_sessions(db, project_ids)),
        ):
            fh.write(f"  \"{label}\": [")
            first = True
            for row in stream:
                if first:
                    fh.write("\n    ")
                    first = False
                else:
                    fh.write(",\n    ")
                fh.write(json.dumps(row, cls=_ExportEncoder))
                counts[label] += 1
            if not first:
                fh.write("\n  ")
            # raw_sessions is the last array; no trailing comma.
            if label == "memory":
                fh.write("],\n")
            else:
                fh.write("]\n")

        fh.write("}\n")
    finally:
        fh.close()

    logger.info(
        "[export] wrote %s — projects=%d devs=%d memory=%d raw_sessions=%d",
        out_path, counts["projects"], counts["devs"],
        counts["memory"], counts["raw_sessions"],
    )
    return counts
