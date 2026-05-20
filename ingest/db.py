"""Database helpers for the ingest pipeline."""

from __future__ import annotations

import psycopg2
import psycopg2.extras

from config import DATABASE_URL
from memory_writer import record_memory

psycopg2.extras.register_uuid()


def get_connection():
    return psycopg2.connect(DATABASE_URL)


def get_project_id(slug: str) -> str | None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM devbrain.projects WHERE slug = %s", (slug,))
        row = cur.fetchone()
        return str(row[0]) if row else None


def session_exists(source_app: str, source_hash: str) -> bool:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM devbrain.raw_sessions WHERE source_app = %s AND source_hash = %s",
            (source_app, source_hash),
        )
        return cur.fetchone() is not None


def get_existing_session_id(source_app: str, session_id: str) -> str | None:
    """Check if a session already exists by app + session_id (not hash)."""
    if not session_id:
        return None
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.raw_sessions WHERE source_app = %s AND session_id = %s ORDER BY created_at DESC LIMIT 1",
            (source_app, session_id),
        )
        row = cur.fetchone()
        return str(row[0]) if row else None


def insert_raw_session(
    *,
    project_id: str | None,
    source_app: str,
    source_path: str,
    source_hash: str,
    session_id: str | None,
    model_used: str | None,
    started_at: str | None,
    ended_at: str | None,
    message_count: int,
    raw_content: str,
    summary: str | None,
    files_touched: list[str],
) -> str:
    with get_connection() as conn, conn.cursor() as cur:
        # Check if this session already exists (by session_id, not hash)
        # If so, UPDATE it instead of creating a duplicate
        existing_id = None
        if session_id:
            cur.execute(
                "SELECT id FROM devbrain.raw_sessions WHERE source_app = %s AND session_id = %s ORDER BY created_at DESC LIMIT 1",
                (source_app, session_id),
            )
            row = cur.fetchone()
            existing_id = row[0] if row else None

        if existing_id:
            # Update existing session with new content
            cur.execute(
                """
                UPDATE devbrain.raw_sessions
                SET source_hash = %s, source_path = %s, message_count = %s,
                    raw_content = %s, ended_at = %s, files_touched = %s::jsonb
                WHERE id = %s
                RETURNING id
                """,
                (
                    source_hash, source_path, message_count,
                    raw_content, ended_at, psycopg2.extras.Json(files_touched),
                    existing_id,
                ),
            )
            row = cur.fetchone()
            conn.commit()
            return str(row[0]) if row else ""
        else:
            # Insert new session
            cur.execute(
                """
                INSERT INTO devbrain.raw_sessions
                    (project_id, source_app, source_path, source_hash, session_id,
                     model_used, started_at, ended_at, message_count, raw_content,
                     summary, files_touched)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (source_app, source_hash) DO NOTHING
                RETURNING id
                """,
                (
                    project_id, source_app, source_path, source_hash, session_id,
                    model_used, started_at, ended_at, message_count, raw_content,
                    summary, psycopg2.extras.Json(files_touched),
                ),
            )
            row = cur.fetchone()
            conn.commit()
            return str(row[0]) if row else ""


def insert_chunk(
    *,
    project_id: str | None,
    source_type: str,
    source_id: str | None,
    source_line_start: int | None,
    source_line_end: int | None,
    content: str,
    embedding: list[float],
    token_count: int,
) -> str:
    vector_str = f"[{','.join(str(v) for v in embedding)}]"
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO devbrain.chunks
                (project_id, source_type, source_id, source_line_start,
                 source_line_end, content, embedding, token_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s::vector, %s)
            RETURNING id
            """,
            (
                project_id, source_type, source_id, source_line_start,
                source_line_end, content, vector_str, token_count,
            ),
        )
        row = cur.fetchone()
        chunk_id = str(row[0]) if row else ""
        # P2.b dual-write: skip when project_id is None (chunks.project_id
        # is nullable but devbrain.memory.project_id is NOT NULL). The
        # SAVEPOINT inside record_memory keeps a memory failure from
        # poisoning this transaction's commit of the legacy chunk row.
        if chunk_id and project_id is not None:
            # provenance_id = the SOURCE session's UUID (chunks.source_id),
            # not the chunk row's own UUID. Migration 032 fixed the
            # historical bug where this was chunk_id; that broke
            # session-grouped atomization in factory/cognify/. When
            # source_id is None (e.g. migrate_openclaw_memory.py imports
            # of pre-chunked markdown), we pass None — the partial
            # unique index on (provenance_id, kind) excludes NULL.
            record_memory(
                cur,
                project_id=project_id,
                kind="chunk",
                content=content,
                embedding_sql=vector_str,
                provenance_id=source_id,
            )
        conn.commit()
        return chunk_id


def delete_chunks_for_session(session_id: str) -> int:
    """Delete all chunks for a session (before re-embedding on update)."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM devbrain.chunks WHERE source_id = %s",
            (session_id,),
        )
        count = cur.rowcount
        conn.commit()
        return count


def delete_session(session_id: str) -> dict[str, int]:
    """Atomically delete a raw_session and all its associated rows.

    The full lineage is `devbrain.raw_sessions.id` (the session UUID)
    → `devbrain.chunks.source_id` → `devbrain.memory.provenance_id`.
    None of those edges has an `ON DELETE CASCADE` foreign key
    (chunks.source_id is polymorphic by design, and memory.provenance_id
    has no FK at all), so deleting a raw_session row in isolation
    silently orphans every chunk and memory row that referenced it.

    On 2026-04-09 a one-off cleanup script removed ~414 duplicate
    raw_sessions but didn't delete the underlying chunks. The cleanup
    is what migration 036 had to undo (reconstruct 296 truly-unique
    ghost sessions + drop 92 duplicate orphan chunks). This helper is
    the sanctioned way to delete a session going forward — use it
    instead of running ad-hoc DELETEs against raw_sessions.

    Returns a dict with per-table counts: `{'memory', 'chunks',
    'raw_sessions'}`. Deletes are best-effort: missing session is not
    an error — the function returns zeros for whichever tables had no
    rows to delete.
    """
    with get_connection() as conn, conn.cursor() as cur:
        # Order matters: memory rows reference chunk_id via provenance_id
        # (pre-migration-032) or source_id (post-032). Either way, the
        # memory row's provenance_id == chunks.source_id == session_id
        # for chunk-kind rows. Delete memory first so the dual-write
        # contract stays consistent during the legacy delete.
        cur.execute(
            "DELETE FROM devbrain.memory WHERE provenance_id = %s",
            (session_id,),
        )
        memory_deleted = cur.rowcount
        cur.execute(
            "DELETE FROM devbrain.chunks WHERE source_id = %s",
            (session_id,),
        )
        chunks_deleted = cur.rowcount
        cur.execute(
            "DELETE FROM devbrain.raw_sessions WHERE id = %s",
            (session_id,),
        )
        sessions_deleted = cur.rowcount
        conn.commit()
        return {
            "memory": memory_deleted,
            "chunks": chunks_deleted,
            "raw_sessions": sessions_deleted,
        }


def update_session_summary(
    session_id: str, summary: str, *, source: str = "ollama",
) -> None:
    """Persist a freshly-generated summary on raw_sessions.

    Args:
        session_id: raw_sessions.id (UUID).
        summary: the new summary text.
        source: which summarizer produced it. The ingest pipeline calls
            with the default 'ollama'; cognify_resummarize passes
            'sonnet' (or 'opus'). Stored on the new summary_source
            column from migration 041 so the resummarize pass can find
            Ollama-source rows that need upgrading.
    """
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.raw_sessions "
            "SET summary = %s, summary_source = %s WHERE id = %s",
            (summary, source, session_id),
        )
        conn.commit()
