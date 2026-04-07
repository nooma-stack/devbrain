#!/usr/bin/env python3
"""Backfill summaries for sessions that were ingested without summarization."""

from __future__ import annotations

from db import get_connection, update_session_summary
from embeddings import embed
from db import insert_chunk
from summarize import summarize_text


def backfill():
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, project_id, raw_content FROM devbrain.raw_sessions "
            "WHERE summary IS NULL ORDER BY started_at DESC"
        )
        rows = cur.fetchall()

    print(f"Found {len(rows)} sessions without summaries")

    for i, (session_id, project_id, raw_content) in enumerate(rows):
        print(f"\n[{i+1}/{len(rows)}] Summarizing session {str(session_id)[:8]}...")
        try:
            summary = summarize_text(raw_content)
            if summary:
                update_session_summary(str(session_id), summary)
                # Store summary as searchable chunk
                summary_embedding = embed(summary)
                vector_str = f"[{','.join(str(v) for v in summary_embedding)}]"
                insert_chunk(
                    project_id=str(project_id) if project_id else None,
                    source_type="session_summary",
                    source_id=str(session_id),
                    source_line_start=None,
                    source_line_end=None,
                    content=summary,
                    embedding=summary_embedding,
                    token_count=len(summary) // 4,
                )
                print(f"  Done ({len(summary)} chars)")
            else:
                print("  Empty summary, skipping")
        except Exception as e:
            print(f"  Failed: {e}")
            continue


if __name__ == "__main__":
    backfill()
