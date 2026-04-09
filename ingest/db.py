"""Database helpers for the ingest pipeline."""

from __future__ import annotations

import psycopg2
import psycopg2.extras

from config import DATABASE_URL

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
        conn.commit()
        return str(row[0]) if row else ""


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


def update_session_summary(session_id: str, summary: str) -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.raw_sessions SET summary = %s WHERE id = %s",
            (summary, session_id),
        )
        conn.commit()
