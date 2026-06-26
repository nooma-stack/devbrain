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


def get_or_create_project_id(slug: str) -> str:
    """Resolve a slug to a project id, creating the project on first sight.

    Supports the auto-project-roots convention: a session arriving from a
    new workspace folder (e.g. lighthouse/website) materializes its
    project row instead of falling into the orphan bucket. Idempotent
    under concurrent ingest via ON CONFLICT.
    """
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO devbrain.projects (slug, name)
            VALUES (%s, %s)
            ON CONFLICT (slug) DO NOTHING
            """,
            (slug, slug),
        )
        conn.commit()
        cur.execute("SELECT id FROM devbrain.projects WHERE slug = %s", (slug,))
        return str(cur.fetchone()[0])


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
    cur=None,
) -> str:
    """Insert or update the raw_sessions row. When `cur` is given, runs on the
    caller's cursor without committing (so raw_session + chunks land in one
    transaction); otherwise self-manages a connection + commit."""
    if cur is not None:
        return _insert_raw_session_on_cursor(
            cur, project_id=project_id, source_app=source_app,
            source_path=source_path, source_hash=source_hash,
            session_id=session_id, model_used=model_used,
            started_at=started_at, ended_at=ended_at,
            message_count=message_count, raw_content=raw_content,
            summary=summary, files_touched=files_touched,
        )
    with get_connection() as conn, conn.cursor() as own_cur:
        result = _insert_raw_session_on_cursor(
            own_cur, project_id=project_id, source_app=source_app,
            source_path=source_path, source_hash=source_hash,
            session_id=session_id, model_used=model_used,
            started_at=started_at, ended_at=ended_at,
            message_count=message_count, raw_content=raw_content,
            summary=summary, files_touched=files_touched,
        )
        own_cur.connection.commit()
        return result


def _insert_raw_session_on_cursor(
    cur,
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
    """raw_sessions insert-or-update on a caller-owned cursor (no commit)."""
    # Check if this session already exists (by session_id, not hash).
    # If so, UPDATE it instead of creating a duplicate.
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
        return str(row[0]) if row else ""


def _insert_chunk_on_cursor(
    cur,
    *,
    project_id: str | None,
    source_type: str,
    source_id: str | None,
    source_line_start: int | None,
    source_line_end: int | None,
    content: str,
    vector_str: str,
    token_count: int,
) -> str:
    """INSERT the chunk + dual-write its memory row on the given cursor.
    Does NOT commit — the caller owns the transaction."""
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
        # not the chunk row's own UUID (migration 032). When source_id is
        # None (markdown imports), we pass None — the partial unique index
        # on (provenance_id, kind) excludes NULL.
        record_memory(
            cur,
            project_id=project_id,
            kind="chunk",
            content=content,
            embedding_sql=vector_str,
            provenance_id=source_id,
        )
    return chunk_id


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
    cur=None,
) -> str:
    """Insert a chunk + dual-write its memory row.

    When `cur` is given, runs on the caller's cursor without committing so a
    whole session can be ingested in one transaction (a crash then leaves the
    session atomically absent rather than half-indexed, which the source-hash
    gate would otherwise never re-complete). When `cur` is None, manages its
    own connection + commit (the legacy per-chunk-durable behavior used by the
    standalone importers)."""
    vector_str = f"[{','.join(str(v) for v in embedding)}]"
    kw = dict(
        project_id=project_id, source_type=source_type, source_id=source_id,
        source_line_start=source_line_start, source_line_end=source_line_end,
        content=content, vector_str=vector_str, token_count=token_count,
    )
    if cur is not None:
        return _insert_chunk_on_cursor(cur, **kw)
    with get_connection() as conn, conn.cursor() as own_cur:
        chunk_id = _insert_chunk_on_cursor(own_cur, **kw)
        conn.commit()
        return chunk_id


def delete_chunks_for_session(session_id: str, cur=None) -> int:
    """Delete all chunks for a session (before re-embedding on update).
    Runs on the caller's cursor (no commit) when `cur` is given."""
    if cur is not None:
        cur.execute("DELETE FROM devbrain.chunks WHERE source_id = %s", (session_id,))
        return cur.rowcount
    with get_connection() as conn, conn.cursor() as own_cur:
        own_cur.execute(
            "DELETE FROM devbrain.chunks WHERE source_id = %s", (session_id,)
        )
        count = own_cur.rowcount
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
