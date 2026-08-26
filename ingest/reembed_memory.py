#!/usr/bin/env python3 -u
"""Re-embed devbrain.memory rows with the current embedding model.

Run after ANY change to the embedding runtime or model — including an
ollama upgrade. Discovered 2026-08-26: the 0.21→0.32 ollama upgrade
shifted snowflake-arctic-embed2's numeric output enough (stored-vs-
recomputed cosine 0.987–0.997 on pre-upgrade rows, 1.000 on post-upgrade
rows) that pre-upgrade memories rank ~1% off against fresh queries, and
freshly-written memories lose to stale clusters inside dense score bands.
deep_search reads devbrain.memory, which the older ingest/reembed.py
(chunks-only, pre-P2) never touches — hence this tool.

Embed-text conventions per kind, matching every writer in the codebase
(mcp-server store/breadcrumb: title\\ncontent; cognify extract + curator:
title\\ncontent when titled; end_session + fanout session summaries and
chunk-kind rows: content only):

    chunk, session_summary        -> content
    everything else               -> title\\ncontent if titled else content

Safe to run against a LIVE database: row-wise UPDATEs, commits per batch,
readers never blocked, and re-embedding an already-current row writes an
identical vector (embedding is deterministic within a runtime generation).

Usage:
    python ingest/reembed_memory.py --cutoff 2026-08-24T19:00:00Z
    python ingest/reembed_memory.py --after-id <uuid>   # resume
    python ingest/reembed_memory.py                     # full corpus
"""

from __future__ import annotations

import argparse
import sys
import time

import psycopg2

from config import DATABASE_URL
from embeddings import embed, embed_batch

# Memory contents run longer than the ~400-token chunks reembed.py was
# tuned for (session summaries and breadcrumbs reach several KB), so the
# batch is smaller; the individual-embed fallback catches any batch that
# still overruns the model's context.
EMBED_BATCH_SIZE = 8
DB_FETCH_SIZE = 200
COMMIT_EVERY = 200


def embed_input(kind: str, title: str | None, content: str) -> str:
    if kind in ("chunk", "session_summary"):
        return content
    return f"{title}\n{content}" if title else content


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutoff", default=None,
                    help="Only rows created before this timestamp (ISO). "
                         "Default: all rows.")
    ap.add_argument("--after-id", default=None,
                    help="Resume: skip rows with id <= this uuid.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    update_cur = conn.cursor()

    where = ["embedding IS NOT NULL"]
    params: list[object] = []
    if args.cutoff:
        where.append("created_at < %s")
        params.append(args.cutoff)
    if args.after_id:
        where.append("id > %s")
        params.append(args.after_id)
    where_sql = " AND ".join(where)

    cur.execute(f"SELECT COUNT(*) FROM devbrain.memory WHERE {where_sql}", params)
    total = cur.fetchone()[0]
    print(f"Re-embedding {total} memory rows "
          f"(cutoff={args.cutoff or 'none'}, batch={EMBED_BATCH_SIZE})...",
          flush=True)
    if args.dry_run:
        return

    cur.execute(
        f"SELECT id, kind, title, content FROM devbrain.memory "
        f"WHERE {where_sql} ORDER BY id", params)

    processed = errors = since_commit = 0
    start = time.time()
    last_id = None

    while True:
        rows = cur.fetchmany(DB_FETCH_SIZE)
        if not rows:
            break
        for i in range(0, len(rows), EMBED_BATCH_SIZE):
            batch = rows[i : i + EMBED_BATCH_SIZE]
            texts = [embed_input(r[1], r[2], r[3] or "") for r in batch]
            try:
                embeddings = embed_batch(texts)
                pairs = list(zip((r[0] for r in batch), embeddings))
            except Exception:
                pairs = []
                for r, text in zip(batch, texts):
                    try:
                        pairs.append((r[0], embed(text)))
                    except Exception as exc:
                        errors += 1
                        print(f"  ERROR {r[0]}: {str(exc)[:120]}", flush=True)
            for mid, emb in pairs:
                vector_str = f"[{','.join(str(v) for v in emb)}]"
                update_cur.execute(
                    "UPDATE devbrain.memory SET embedding = %s::vector WHERE id = %s",
                    (vector_str, mid))
                last_id = mid
            processed += len(pairs)
            since_commit += len(pairs)
            if since_commit >= COMMIT_EVERY:
                conn.commit()
                since_commit = 0
        rate = processed / max(time.time() - start, 1)
        eta_min = (total - processed) / max(rate, 0.1) / 60
        print(f"  {processed}/{total} ({rate:.0f}/s, ~{eta_min:.0f}min left, "
              f"errors={errors}, last_id={last_id})", flush=True)

    conn.commit()
    print(f"DONE: {processed} re-embedded, {errors} errors, "
          f"{(time.time() - start)/60:.1f} min", flush=True)


if __name__ == "__main__":
    sys.exit(main())
