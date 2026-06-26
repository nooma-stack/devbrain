#!/usr/bin/env python3
"""Backfill embeddings for devbrain.memory rows that have none.

Several writers used to insert atoms (decision/pattern/lesson) without an
embedding, leaving them invisible to deep_search (which filters
`embedding IS NOT NULL`). The code paths are fixed (cognify.embedding); this
script backfills the rows that already exist.

Idempotent + resumable: only touches rows where embedding IS NULL AND
archived_at IS NULL. Re-run any time. Embeds `title\\ncontent` (matching how
store()/extract embed atoms) in batches via the local Ollama model.

Usage:
    python scripts/reembed_memory.py [--project SLUG] [--batch 32] [--limit N]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# ingest/embeddings.py does a bare `from config import ...`, so ingest/ must
# be the FIRST sys.path entry — adding factory/ too would shadow ingest's
# config.py with factory/config.py (which lacks EMBED_MODEL). ingest is all
# this script needs.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "ingest"))

import psycopg2  # noqa: E402

from embeddings import embed_batch  # ingest/embeddings.py  # noqa: E402


def _database_url() -> str:
    url = os.environ.get("DEVBRAIN_DATABASE_URL")
    if url:
        return url
    pw = os.environ["DEVBRAIN_DB_PASSWORD"]
    host = os.environ.get("DEVBRAIN_DB_HOST", "127.0.0.1")
    port = os.environ.get("DEVBRAIN_DB_HOST_PORT", "5433")
    name = os.environ.get("DEVBRAIN_DB_NAME", "devbrain")
    user = os.environ.get("DEVBRAIN_DB_USER", "devbrain")
    return f"postgresql://{user}:{pw}@{host}:{port}/{name}"


def _vec(emb: list[float]) -> str:
    return "[" + ",".join(str(x) for x in emb) + "]"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=None, help="project slug to scope to")
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--limit", type=int, default=0, help="0 = all")
    args = ap.parse_args()

    conn = psycopg2.connect(_database_url())
    conn.autocommit = False

    where = "embedding IS NULL AND archived_at IS NULL"
    params: list = []
    if args.project:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM devbrain.projects WHERE slug = %s", (args.project,))
            row = cur.fetchone()
            if not row:
                print(f"project {args.project!r} not found", file=sys.stderr)
                return 2
        where += " AND project_id = %s"
        params.append(row[0])

    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM devbrain.memory WHERE {where}", params)
        total = cur.fetchone()[0]
    print(f"{total} rows need embedding" + (f" (project={args.project})" if args.project else ""))
    if total == 0:
        return 0

    done = 0
    failed = 0
    started = time.time()
    while True:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT id, title, content FROM devbrain.memory WHERE {where} "
                f"ORDER BY id LIMIT %s",
                params + [args.batch],
            )
            rows = cur.fetchall()
        if not rows:
            break
        texts = [
            (f"{t}\n{c}" if t else (c or "")) for (_id, t, c) in rows
        ]
        try:
            embs = embed_batch(texts)
        except Exception as exc:  # noqa: BLE001
            print(f"  batch embed failed ({exc}); retrying one-by-one", file=sys.stderr)
            embs = []
            from embeddings import embed as _embed  # noqa: PLC0415
            for tx in texts:
                try:
                    embs.append(_embed(tx))
                except Exception:
                    embs.append(None)
        with conn.cursor() as cur:
            for (mid, _t, _c), emb in zip(rows, embs):
                if emb is None:
                    failed += 1
                    continue
                cur.execute(
                    "UPDATE devbrain.memory SET embedding = %s::vector WHERE id = %s",
                    (_vec(emb), mid),
                )
                done += 1
        conn.commit()
        if args.limit and done >= args.limit:
            break
        rate = done / max(1e-6, time.time() - started)
        print(f"  embedded {done}/{total} ({rate:.0f}/s), {failed} failed", flush=True)

    print(f"done: embedded {done}, failed {failed}, elapsed {time.time()-started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
