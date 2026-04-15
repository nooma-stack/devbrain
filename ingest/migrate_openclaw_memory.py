#!/usr/bin/env python3
"""Migrate OpenClaw's FTS memory chunks to DevBrain.

OpenClaw stores pre-chunked text entries in ~/.openclaw/memory/main.sqlite
as FTS-only (no vector embeddings). This script reads them, embeds them via
Ollama, and stores them in DevBrain's chunks table.

Usage:
    python migrate_openclaw_memory.py <project_slug>
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from db import get_project_id, insert_chunk
from embeddings import embed

OPENCLAW_DB = Path.home() / ".openclaw" / "memory" / "main.sqlite"


def migrate(project_slug: str):
    if not OPENCLAW_DB.exists():
        print(f"OpenClaw memory DB not found: {OPENCLAW_DB}")
        return

    project_id = get_project_id(project_slug)
    if not project_id:
        print(f"Project slug not found in DevBrain: {project_slug}")
        return

    conn = sqlite3.connect(str(OPENCLAW_DB))
    cur = conn.cursor()

    # Get all chunks from the embedding_cache table
    cur.execute("SELECT rowid, embedding FROM embedding_cache")
    rows = cur.fetchall()
    print(f"Found {len(rows)} OpenClaw memory entries")

    # The embedding_cache stores text content that was indexed for FTS
    # Let's check the chunks table instead which has the actual text
    cur.execute("SELECT rowid, * FROM chunks LIMIT 1")
    sample = cur.fetchone()
    if sample:
        cols = [d[0] for d in cur.description]
        print(f"Chunks table columns: {cols}")

    cur.execute("SELECT count(*) FROM chunks")
    chunk_count = cur.fetchone()[0]
    print(f"Chunks table has {chunk_count} rows")

    if chunk_count == 0:
        print("No chunks to migrate — checking embedding_cache instead")
        # embedding_cache might have the text
        cur.execute("SELECT count(*) FROM embedding_cache")
        cache_count = cur.fetchone()[0]
        print(f"embedding_cache has {cache_count} entries")

        if cache_count > 0:
            # In FTS-only mode, the 'embedding' column stores the text content
            cur.execute("SELECT rowid, embedding FROM embedding_cache")
            all_entries = cur.fetchall()

            migrated = 0
            for rowid, text_content in all_entries:
                if not text_content or not isinstance(text_content, str) or len(text_content.strip()) < 20:
                    continue

                text_content = text_content.replace("\x00", "")
                # Truncate very long entries for embedding
                embed_text = text_content[:2000]

                try:
                    embedding = embed(embed_text)
                    insert_chunk(
                        project_id=project_id,
                        source_type="openclaw_memory",
                        source_id=None,
                        source_line_start=None,
                        source_line_end=None,
                        content=text_content[:5000],  # Cap stored content
                        embedding=embedding,
                        token_count=len(text_content) // 4,
                    )
                    migrated += 1

                    if migrated % 200 == 0:
                        print(f"  ...{migrated}/{cache_count}")
                except Exception as e:
                    print(f"  Error migrating entry {rowid}: {e}")
                    continue

            conn.close()
            print(f"\nMigrated {migrated} OpenClaw memory entries to DevBrain")
            return

        conn.close()
        return

    # Read chunks and migrate
    cur.execute("SELECT rowid, * FROM chunks")
    all_chunks = cur.fetchall()
    cols = [d[0] for d in cur.description]

    # Find the text content column
    text_col_idx = None
    for i, col in enumerate(cols):
        if col in ("content", "text", "chunk"):
            text_col_idx = i
            break

    if text_col_idx is None:
        print(f"Could not find text column. Columns: {cols}")
        conn.close()
        return

    migrated = 0
    for row in all_chunks:
        text = row[text_col_idx]
        if not text or not isinstance(text, str) or len(text.strip()) < 20:
            continue

        text = text.replace("\x00", "")  # Strip NUL bytes

        try:
            embedding = embed(text[:2000])  # Limit text length for embedding
            insert_chunk(
                project_id=project_id,
                source_type="openclaw_memory",
                source_id=None,
                source_line_start=None,
                source_line_end=None,
                content=text,
                embedding=embedding,
                token_count=len(text) // 4,
            )
            migrated += 1

            if migrated % 100 == 0:
                print(f"  ...{migrated}/{len(all_chunks)}")
        except Exception as e:
            print(f"  Error migrating chunk {row[0]}: {e}")
            continue

    conn.close()
    print(f"\nMigrated {migrated} OpenClaw memory chunks to DevBrain")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python migrate_openclaw_memory.py <project_slug>")
        sys.exit(1)
    migrate(sys.argv[1])
