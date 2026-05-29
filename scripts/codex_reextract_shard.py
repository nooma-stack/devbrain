#!/usr/bin/env python
"""ADD-mode codex re-extraction over real sessions — shardable for parallelism.

Recovers atoms lost in the 2026-05-28 dedup incident by re-extracting each
REAL session (raw_sessions-backed) with the codex backend, in ADD mode
(reextract_mode=False) so a failed/empty extraction can NEVER archive a
surviving atom — there is no net-loss path. Re-runs are idempotent: the
(provenance_id, kind, title) index folds identical re-extractions, and new
distinct atoms are appended.

Parallelism: run M instances concurrently, each owning a disjoint stride-slice
and its own checkpoint file:
    python codex_reextract_shard.py --project=brightbot --shard=0/5
    python codex_reextract_shard.py --project=brightbot --shard=1/5
    ... (through 4/5)

Resumable: a crash/Ctrl-C leaves a checkpoint at
~/.devbrain/cognify-bulk-<project>-shard-N-of-M.json; re-run the same command.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

FACTORY = str(Path(__file__).resolve().parent.parent / "factory")
sys.path.insert(0, FACTORY)

from config import DATABASE_URL  # noqa: E402
from state_machine import FactoryDB  # noqa: E402
from cognify.bulk import (  # noqa: E402
    apply_shard,
    discover_all_sessions_with_chunks,
    render_stderr_progress,
    run_bulk,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--shard", default=None, help="N/M (e.g. 0/5)")
    ap.add_argument("--max-llm-calls", type=int, default=None)
    ap.add_argument("--model", default="codex",
                    help="Extraction model (default: codex CLI backend).")
    args = ap.parse_args()

    shard = None
    if args.shard:
        n_str, m_str = args.shard.split("/", 1)
        shard = (int(n_str), int(m_str))

    db = FactoryDB(DATABASE_URL)
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM devbrain.projects WHERE slug = %s", (args.project,))
        row = cur.fetchone()
    if not row:
        sys.exit(f"project {args.project!r} not found")
    project_id = row[0]

    with db._conn() as conn:
        sessions = discover_all_sessions_with_chunks(conn, project_id)
        total = len(sessions)
        if shard:
            sessions = apply_shard(sessions, shard)
        sys.stderr.write(
            f"codex-reextract: project={args.project} shard={args.shard or 'none'} "
            f"sessions={len(sessions)} (of {total} real sessions) model={args.model} "
            f"mode=ADD(no-archive)\n"
        )
        sys.stderr.flush()
        res = run_bulk(
            conn, project_id, args.project,
            sessions=sessions,
            reextract_mode=False,          # ADD mode — never archive; no net-loss
            model=args.model,
            shard=shard,
            max_llm_calls=args.max_llm_calls,
            progress_callback=render_stderr_progress,
        )

    print(json.dumps({
        "shard": args.shard,
        "sessions_targeted": res.sessions_targeted,
        "sessions_processed": res.sessions_processed,
        "sessions_skipped_resume": res.sessions_skipped_resume,
        "atoms_created": res.atoms_created,
        "sessions_failed": res.sessions_failed,
        "failure_counts": res.failure_counts,
        "halted_early": res.halted_early,
        "elapsed_seconds": res.elapsed_seconds,
    }, indent=2))


if __name__ == "__main__":
    main()
