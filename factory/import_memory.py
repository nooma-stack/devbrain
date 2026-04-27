"""Import a DevBrain export into the local database (#5.b).

Pairs with :mod:`factory.export_memory`. Reads the JSON document
produced by ``write_export_file``/``export_to_dict`` and lands it in
the four target tables — ``projects``, ``devs``, ``memory``, and
``raw_sessions``.

Idempotency
-----------
Every per-table insert uses ``ON CONFLICT DO NOTHING`` keyed on the
table's natural identifier so a re-run of the same export doesn't
duplicate or corrupt anything:

* ``projects.slug`` — UNIQUE.
* ``devs.dev_id`` — UNIQUE; preserves any *locally* customized
  channels / event_subscriptions (PR #38 posture). The export file's
  channels are *only* used when the dev_id is unknown to the
  destination; otherwise we leave the local row alone.
* ``memory (provenance_id, kind)`` — partial UNIQUE index from
  migration 011. Rows with NULL provenance_id are inserted
  unconditionally — same behavior as the legacy/dual-write path, since
  the index can't dedupe what it can't see.
* ``raw_sessions (source_app, source_hash)`` — the actual UNIQUE in
  migrations/001:45. The plan spec mentioned ``id`` but ids differ
  across instances, so we conflict on the natural key the source DB
  was already using to dedupe ingestion.

Project-id remapping
--------------------
UUIDs differ between instances, so the export carries
``project_slug`` alongside ``project_id`` for memory and
raw_sessions. On import we look up the destination's project_id by
slug. If the slug isn't already in ``devbrain.projects`` we create the
row from the export's ``projects`` array (or with a minimal stub if
the source export didn't include the project — defensive only).

provenance_id is *not* rewritten. P2.d.i made it a loose pointer (no
FK), so chunk/decision/pattern/issue ids from the source DB simply
land as-is. They lose their meaning as cross-table foreign keys but
keep their meaning as a stable group key for the partial unique
index — which is all the importer actually needs.

Single transaction
------------------
All four inserts (projects → devs → raw_sessions → memory) run inside
*one* connection-level transaction so a mid-load failure doesn't
leave the destination half-updated. memory's project_id is a hard FK
to projects.id; ordering matters.
"""
from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Wire format version we know how to read. Bump in lockstep with
# export_memory.EXPORT_VERSION when the on-disk shape changes.
SUPPORTED_VERSION = 1


# ─── helpers ────────────────────────────────────────────────────────────────


def _check_schema_compat(db, payload: dict) -> None:
    """Reject exports that won't slot cleanly into this destination.

    Two checks:

    1. ``payload['version']`` must equal :data:`SUPPORTED_VERSION` —
       the wire format. Different versions have different row shapes.
    2. ``payload['source']['schema_migration_top']`` must equal the
       destination's highest applied migration filename. Cross-version
       imports might silently land NULL into a NOT NULL column added
       in a newer migration, or skip a constraint a newer index
       depends on. The destination operator should run
       ``bin/devbrain migrate`` first.

    Raises ``ValueError`` with an actionable message on mismatch.
    Past-review lesson (factory_review pattern, 2026-04-23): surface
    schema drift loudly at the entry point, not as a buried INSERT
    failure six tables in.
    """
    version = payload.get("version")
    if version != SUPPORTED_VERSION:
        raise ValueError(
            f"unsupported export version {version!r} "
            f"(this build reads version {SUPPORTED_VERSION}). "
            "Re-export from a matching DevBrain build."
        )

    source_top = (payload.get("source") or {}).get("schema_migration_top")
    dest_top = _highest_migration(db)

    if source_top is None:
        raise ValueError(
            "export has no schema_migration_top — produced by a pre-009 "
            "DevBrain install we can't safely match. Re-export after "
            "running `bin/devbrain migrate` on the source."
        )
    if dest_top is None:
        raise ValueError(
            "destination has no schema_migration_top — run "
            "`bin/devbrain migrate` first to bring the schema up."
        )
    if source_top != dest_top:
        raise ValueError(
            "schema mismatch: export was produced against "
            f"{source_top!r}, this destination is at {dest_top!r}. "
            "Run `bin/devbrain migrate` on whichever side is older "
            "before importing."
        )


def _highest_migration(db) -> str | None:
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


def _upsert_projects(cur, projects: list[dict]) -> dict[str, str]:
    """INSERT … ON CONFLICT (slug) DO NOTHING → return slug → dest id.

    Existing local projects are *not* overwritten; their pre-import
    metadata wins. Returning the destination id under the export's
    slug gives the caller everything needed to remap memory /
    raw_sessions FK references.
    """
    slug_to_id: dict[str, str] = {}
    for p in projects:
        cur.execute(
            """
            INSERT INTO devbrain.projects
                (slug, name, root_path, description, constraints,
                 tech_stack, lint_commands, test_commands, metadata)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s::jsonb)
            ON CONFLICT (slug) DO NOTHING
            RETURNING id
            """,
            (
                p["slug"],
                p.get("name") or p["slug"],
                p.get("root_path"),
                p.get("description"),
                json.dumps(p.get("constraints") or []),
                json.dumps(p.get("tech_stack") or {}),
                json.dumps(p.get("lint_commands") or {}),
                json.dumps(p.get("test_commands") or {}),
                json.dumps(p.get("metadata") or {}),
            ),
        )
        row = cur.fetchone()
        if row is not None:
            slug_to_id[p["slug"]] = str(row[0])
        else:
            # Conflict on slug — fetch the existing id so memory rows
            # remap to the destination's row, not the source's.
            cur.execute(
                "SELECT id FROM devbrain.projects WHERE slug = %s",
                (p["slug"],),
            )
            slug_to_id[p["slug"]] = str(cur.fetchone()[0])
    return slug_to_id


def _ensure_project(cur, slug: str, slug_to_id: dict[str, str]) -> str:
    """Resolve slug → destination project_id, creating a stub if needed.

    A memory or raw_sessions row whose slug isn't covered by the
    export's projects array can still be imported — we just don't have
    rich metadata for the project. Falling through with a stub keeps
    "unknown project" exports working instead of refusing them.
    """
    if slug in slug_to_id:
        return slug_to_id[slug]
    cur.execute(
        """
        INSERT INTO devbrain.projects (slug, name)
        VALUES (%s, %s)
        ON CONFLICT (slug) DO NOTHING
        RETURNING id
        """,
        (slug, slug),
    )
    row = cur.fetchone()
    if row is not None:
        new_id = str(row[0])
    else:
        cur.execute(
            "SELECT id FROM devbrain.projects WHERE slug = %s", (slug,)
        )
        new_id = str(cur.fetchone()[0])
    slug_to_id[slug] = new_id
    return new_id


def _upsert_devs(cur, devs: list[dict]) -> dict[str, int]:
    """Insert dev rows that don't exist locally; leave existing rows alone.

    PR #38 posture: re-running install / import must not overwrite
    user-customized channels or event_subscriptions. Concretely, we
    ``ON CONFLICT (dev_id) DO NOTHING`` — if the local row already
    exists, the local channels/full_name/event_subscriptions win.
    """
    counts = {"inserted": 0, "preserved": 0}
    for d in devs:
        cur.execute(
            """
            INSERT INTO devbrain.devs
                (dev_id, full_name, channels, event_subscriptions)
            VALUES (%s, %s, %s::jsonb, %s::jsonb)
            ON CONFLICT (dev_id) DO NOTHING
            """,
            (
                d["dev_id"],
                d.get("full_name"),
                json.dumps(d.get("channels") or []),
                json.dumps(
                    d.get("event_subscriptions")
                    or [
                        "job_ready", "job_failed", "lock_conflict",
                        "unblocked", "needs_human",
                    ]
                ),
            ),
        )
        if cur.rowcount == 1:
            counts["inserted"] += 1
        else:
            counts["preserved"] += 1
    return counts


def _insert_memory(
    cur, memory: list[dict], slug_to_id: dict[str, str],
) -> dict[str, int]:
    """Insert memory rows, ON CONFLICT on (provenance_id, kind).

    Embedding round-trips via the pgvector text literal — bit-equal to
    the source on re-export. ``applies_when`` keeps its JSON shape.
    """
    counts = {"scanned": 0, "inserted": 0, "skipped_dup": 0}
    sql = """
        INSERT INTO devbrain.memory
            (project_id, kind, title, content, embedding,
             strength, hit_count, last_hit, applies_when,
             provenance_id, tier, archived_at, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s::vector,
                %s, %s, %s, %s::jsonb,
                %s, %s, %s, %s, %s)
        ON CONFLICT (provenance_id, kind) WHERE provenance_id IS NOT NULL
        DO NOTHING
    """
    for m in memory:
        counts["scanned"] += 1
        slug = m.get("project_slug")
        if not slug:
            # devbrain.memory.project_id is NOT NULL — skip orphans.
            counts["skipped_dup"] += 1
            continue
        project_id = _ensure_project(cur, slug, slug_to_id)

        applies_when = m.get("applies_when")
        if applies_when is not None and not isinstance(applies_when, str):
            applies_when = json.dumps(applies_when)

        cur.execute(
            sql,
            (
                project_id,
                m["kind"],
                m.get("title"),
                m["content"],
                m.get("embedding_text"),
                m.get("strength", 1.0),
                m.get("hit_count", 0),
                m.get("last_hit"),
                applies_when,
                m.get("provenance_id"),
                m.get("tier", "memory"),
                m.get("archived_at"),
                m.get("created_at"),
                m.get("updated_at") or m.get("created_at"),
            ),
        )
        if cur.rowcount == 1:
            counts["inserted"] += 1
        else:
            counts["skipped_dup"] += 1
    return counts


def _insert_raw_sessions(
    cur, raw_sessions: list[dict], slug_to_id: dict[str, str],
) -> dict[str, int]:
    """Insert raw_sessions rows, ON CONFLICT on (source_app, source_hash).

    The plan spec mentioned ``id`` for the conflict key, but ids differ
    across instances. The actual UNIQUE in migrations/001 is
    (source_app, source_hash) — that's the natural key ingest already
    uses to dedupe re-reads of the same transcript file, and it's the
    only key whose value carries over from source to destination.

    project_id can be NULL — orphan sessions are imported as-is so the
    destination can re-run ``attribute-orphans`` with its own
    ``factory.project_paths`` mapping.
    """
    counts = {"scanned": 0, "inserted": 0, "skipped_dup": 0}
    sql = """
        INSERT INTO devbrain.raw_sessions
            (project_id, source_app, source_path, source_hash,
             session_id, model_used, started_at, ended_at,
             message_count, raw_content, summary, files_touched,
             metadata, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s::jsonb, %s::jsonb, %s)
        ON CONFLICT (source_app, source_hash) DO NOTHING
    """
    for r in raw_sessions:
        counts["scanned"] += 1
        project_id = None
        slug = r.get("project_slug")
        if slug:
            project_id = _ensure_project(cur, slug, slug_to_id)

        files_touched = r.get("files_touched")
        if files_touched is not None and not isinstance(files_touched, str):
            files_touched = json.dumps(files_touched)
        metadata = r.get("metadata")
        if metadata is not None and not isinstance(metadata, str):
            metadata = json.dumps(metadata)

        cur.execute(
            sql,
            (
                project_id,
                r["source_app"],
                r["source_path"],
                r["source_hash"],
                r.get("session_id"),
                r.get("model_used"),
                r.get("started_at"),
                r.get("ended_at"),
                r.get("message_count"),
                r.get("raw_content", ""),
                r.get("summary"),
                files_touched,
                metadata,
                r.get("created_at"),
            ),
        )
        if cur.rowcount == 1:
            counts["inserted"] += 1
        else:
            counts["skipped_dup"] += 1
    return counts


# ─── public API ─────────────────────────────────────────────────────────────


def read_import_file(path: Path | str) -> dict:
    """Load an export file from disk. Auto-detects gzip via the suffix."""
    path = Path(path)
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def import_from_dict(
    db,
    payload: dict,
    *,
    dry_run: bool = False,
) -> dict:
    """Land an in-memory export payload into the destination DB.

    Wraps every write in a single transaction. On dry_run, the
    transaction is rolled back at the end so the caller still sees
    realistic counts without committing anything.

    Returns a dict shaped::

        {
          "projects": {"slug_to_id": {...}, "count": N},
          "devs": {"inserted": N, "preserved": N},
          "raw_sessions": {"scanned": N, "inserted": N, "skipped_dup": N},
          "memory": {"scanned": N, "inserted": N, "skipped_dup": N},
          "dry_run": bool,
        }
    """
    _check_schema_compat(db, payload)

    projects = payload.get("projects") or []
    devs = payload.get("devs") or []
    memory = payload.get("memory") or []
    raw_sessions = payload.get("raw_sessions") or []

    results: dict = {"dry_run": dry_run}

    # All four writes share one connection so a failure rolls back the
    # batch atomically. raw_sessions is loaded before memory because
    # memory.provenance_id may point at raw_sessions ids — they have no
    # FK between them today, but ordering keeps the breadcrumb honest
    # for any future tightening of that pointer.
    conn = db._conn()
    try:
        with conn.cursor() as cur:
            slug_to_id = _upsert_projects(cur, projects)
            results["projects"] = {
                "slug_to_id": slug_to_id,
                "count": len(slug_to_id),
            }

            results["devs"] = _upsert_devs(cur, devs)
            results["raw_sessions"] = _insert_raw_sessions(
                cur, raw_sessions, slug_to_id,
            )
            results["memory"] = _insert_memory(cur, memory, slug_to_id)

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    logger.info(
        "[import] projects=%d devs=%d/%d raw_sessions=%d/%d memory=%d/%d "
        "(dry_run=%s)",
        results["projects"]["count"],
        results["devs"]["inserted"], results["devs"]["preserved"],
        results["raw_sessions"]["inserted"], results["raw_sessions"]["scanned"],
        results["memory"]["inserted"], results["memory"]["scanned"],
        dry_run,
    )
    return results
