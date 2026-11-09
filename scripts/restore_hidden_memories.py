#!/usr/bin/env python3
"""Bring back memories the retired cognify-gc pass hid, and repair strength.

cognify-gc archived (set archived_at on) memories idle 90 days with no
outgoing links; every search filters ``archived_at IS NULL``, so they were
invisible. cognify-decay compounded hourly, so "strength < 0.1" was true of
any row idle 30+ days. Both are fixed; this script undoes their effects.

1. Unarchive rows whose archived_at falls inside a completed gc run in
   cognify_run_log. Rows archived by anything else (supersession,
   re-extraction, project archive) are left as they are.
   Exception: a fanout session_summary that fanout already rebuilt (an
   active summary exists for the same source session, project and kind)
   stays archived, since the rebuild replaces it.
2. Reset strengths left at the 0.001 floor by the compounding bug to the
   ceiling the fixed decay gives their idle time (1.0 / 0.5 / 0.12).

Dry run by default; pass --apply to write. One transaction. Every UPDATE
fires the memory_ledger trigger, so each change is audited. Re-runnable.

Usage:
    python scripts/restore_hidden_memories.py [--apply]
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg2

FLOOR = 0.0011  # rows at the old MIN_STRENGTH (0.001)
IDLE = "GREATEST(m.last_cascade_at, m.last_hit, m.created_at)"

GC_ARCHIVED = """
CREATE TEMP TABLE gc_archived ON COMMIT DROP AS
SELECT DISTINCT m.id
FROM devbrain.memory m
JOIN devbrain.cognify_run_log r
  ON r.pass_name = 'gc'
 AND r.completed_at IS NOT NULL
 AND m.archived_at BETWEEN r.started_at AND r.completed_at
WHERE m.archived_at IS NOT NULL
"""

REBUILT = """
SELECT g.id
FROM gc_archived g
JOIN devbrain.memory m ON m.id = g.id
WHERE m.fanout_source_session_id IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM devbrain.memory a
      WHERE a.archived_at IS NULL
        AND a.fanout_source_session_id = m.fanout_source_session_id
        AND a.project_id = m.project_id
        AND a.kind = m.kind
  )
"""

UNARCHIVE = """
UPDATE devbrain.memory m
SET archived_at = NULL
FROM gc_archived g
WHERE m.id = g.id AND NOT (m.id = ANY(%(keep)s::uuid[]))
"""

REPAIR_STRENGTH = (
    "UPDATE devbrain.memory m SET strength = CASE"
    "  WHEN " + IDLE + " < NOW() - make_interval(days => 90) THEN 0.12"
    "  WHEN " + IDLE + " < NOW() - make_interval(days => 30) THEN 0.50"
    "  ELSE 1.0 END "
    "WHERE m.archived_at IS NULL AND m.strength <= %(floor)s"
)


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args()

    conn = psycopg2.connect(_database_url())
    try:
        with conn.cursor() as cur:
            cur.execute(GC_ARCHIVED)
            cur.execute("SELECT count(*) FROM gc_archived")
            hidden = cur.fetchone()[0]
            cur.execute(
                "SELECT m.kind, count(*) FROM gc_archived g "
                "JOIN devbrain.memory m ON m.id = g.id GROUP BY 1 ORDER BY 2 DESC"
            )
            by_kind = cur.fetchall()
            cur.execute(REBUILT)
            keep = [str(r[0]) for r in cur.fetchall()]

            print(f"hidden by gc: {hidden}")
            for kind, n in by_kind:
                print(f"  {kind}: {n}")
            print(f"left archived (fanout already rebuilt them): {len(keep)}")

            cur.execute(UNARCHIVE, {"keep": keep})
            print(f"unarchived: {cur.rowcount}")
            cur.execute(REPAIR_STRENGTH, {"floor": FLOOR})
            print(f"strength repaired: {cur.rowcount}")

        if args.apply:
            conn.commit()
            print("applied")
        else:
            conn.rollback()
            print("dry run: rolled back (pass --apply to write)")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
