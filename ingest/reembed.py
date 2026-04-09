#!/usr/bin/env python3
"""Re-embed all chunks with the current embedding model.

Run after changing the embedding model in config/devbrain.yaml.
Processes in batches to avoid memory issues.

Usage: python ingest/reembed.py
"""

from __future__ import annotations

import sys
import time

import psycopg2

from config import DATABASE_URL
from embeddings import embed

BATCH_SIZE = 100


def main() -> None:
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM devbrain.chunks")
    total = cur.fetchone()[0]
    print(f"Re-embedding {total} chunks...")

    cur.execute("SELECT id, content FROM devbrain.chunks ORDER BY id")
    processed = 0
    errors = 0
    start = time.time()

    while True:
        rows = cur.fetchmany(BATCH_SIZE)
        if not rows:
            break

        for chunk_id, content in rows:
            try:
                embedding = embed(content)
                vector_str = f"[{','.join(str(v) for v in embedding)}]"
                cur2 = conn.cursor()
                cur2.execute(
                    "UPDATE devbrain.chunks SET embedding = %s::vector WHERE id = %s",
                    (vector_str, chunk_id),
                )
                cur2.close()
            except Exception as e:
                errors += 1
                if errors <= 5:
                    print(f"  Error on chunk {chunk_id}: {e}")

            processed += 1
            if processed % 100 == 0:
                conn.commit()
                elapsed = time.time() - start
                rate = processed / elapsed
                remaining = (total - processed) / rate if rate > 0 else 0
                print(f"  {processed}/{total} ({processed*100//total}%) — {rate:.1f}/s — ~{remaining:.0f}s remaining — {errors} errors")

    conn.commit()
    cur.close()
    conn.close()

    elapsed = time.time() - start
    print(f"\nDone: {processed} chunks re-embedded in {elapsed:.0f}s ({errors} errors)")


if __name__ == "__main__":
    main()
