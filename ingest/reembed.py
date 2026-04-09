#!/usr/bin/env python3 -u
"""Re-embed all chunks with the current embedding model.

Run after changing the embedding model in config/devbrain.yaml.
Uses batch embedding for speed (~10x faster than individual).

Usage: python ingest/reembed.py
"""

from __future__ import annotations

import sys
import time

import psycopg2

from config import DATABASE_URL
from embeddings import embed_batch

# Ollama batch size — 10 chunks at ~400 tokens each is well within
# snowflake-arctic-embed2's 8192 token context
EMBED_BATCH_SIZE = 20
DB_FETCH_SIZE = 200


def main() -> None:
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    update_cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM devbrain.chunks")
    total = cur.fetchone()[0]
    print(f"Re-embedding {total} chunks with batch size {EMBED_BATCH_SIZE}...", flush=True)

    cur.execute("SELECT id, content FROM devbrain.chunks ORDER BY id")
    processed = 0
    errors = 0
    start = time.time()

    while True:
        rows = cur.fetchmany(DB_FETCH_SIZE)
        if not rows:
            break

        # Process in embedding batches
        for i in range(0, len(rows), EMBED_BATCH_SIZE):
            batch = rows[i : i + EMBED_BATCH_SIZE]
            ids = [row[0] for row in batch]
            texts = [row[1] for row in batch]

            try:
                embeddings = embed_batch(texts)

                for chunk_id, emb in zip(ids, embeddings):
                    vector_str = f"[{','.join(str(v) for v in emb)}]"
                    update_cur.execute(
                        "UPDATE devbrain.chunks SET embedding = %s::vector WHERE id = %s",
                        (vector_str, chunk_id),
                    )
                processed += len(batch)
            except Exception as e:
                # Fall back to individual on batch failure
                for chunk_id, text in zip(ids, texts):
                    try:
                        from embeddings import embed
                        emb = embed(text)
                        vector_str = f"[{','.join(str(v) for v in emb)}]"
                        update_cur.execute(
                            "UPDATE devbrain.chunks SET embedding = %s::vector WHERE id = %s",
                            (vector_str, chunk_id),
                        )
                        processed += 1
                    except Exception as inner_e:
                        errors += 1
                        if errors <= 5:
                            print(f"  Error on chunk {chunk_id}: {inner_e}", flush=True)
                        processed += 1

        # Commit after each DB fetch batch
        conn.commit()
        elapsed = time.time() - start
        rate = processed / elapsed if elapsed > 0 else 0
        remaining = (total - processed) / rate if rate > 0 else 0
        print(
            f"  {processed}/{total} ({processed*100//total}%) — "
            f"{rate:.1f}/s — ~{remaining:.0f}s remaining — {errors} errors",
            flush=True,
        )

    conn.commit()
    cur.close()
    update_cur.close()
    conn.close()

    elapsed = time.time() - start
    print(f"\nDone: {processed} chunks re-embedded in {elapsed:.0f}s ({errors} errors)", flush=True)


if __name__ == "__main__":
    main()
