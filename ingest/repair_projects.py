#!/usr/bin/env python3
"""Attach project-less sessions to their project and write their missing
memory rows.

A session whose path matched no project mapping at first ingest was stored
with raw_sessions.project_id and chunks.project_id NULL. The memory
dual-write needs a project (memory.project_id is NOT NULL), so those chunks
never reached devbrain.memory: deep_search, extraction and fan-out never
saw them.

For each such session this script:
  1. skips it when the CURRENT exclusion rules (ingest.exclude_path_parts,
     e.g. subagents, a support runner's project dir) would reject its path;
  2. re-resolves the project from the path with the current
     ingest.project_mappings / auto_project_roots (Codex reads the cwd from
     the source file, so a Codex session whose file is gone stays put);
  3. sets the project on the raw session and its chunks (only where NULL);
  4. inserts one memory row per legacy chunk, COPYING the chunk's stored
     embedding (nothing is re-embedded; a chunk without an embedding gets a
     memory row without one, for the reembed pass) and the injection-
     screening flags of the messages it overlaps.

Idempotent (ON CONFLICT on the chunk dedup index). Read-only unless --apply.
Throttled: --rows-per-second caps the insert rate (memory inserts also
write hash-chained ledger rows).

    cd ~/devbrain/ingest
    ../.venv/bin/python repair_projects.py                     # report only
    ../.venv/bin/python repair_projects.py --session ae9bc8fc --apply
    ../.venv/bin/python repair_projects.py --all --apply --rows-per-second 300

No model is called. The ingest watcher also runs step 3-4 itself when it
sees an already-ingested, project-less file that now resolves.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import screening
from db import get_connection, get_or_create_project_id

from config import SCREENING_ENABLED

# Raw sessions with no project, or with chunks that have none.
_CANDIDATES_SQL = """
SELECT r.id::text, r.source_app, r.source_path, r.project_id::text
FROM devbrain.raw_sessions r
WHERE (r.project_id IS NULL
       OR EXISTS (SELECT 1 FROM devbrain.chunks c
                  WHERE c.source_id = r.id AND c.project_id IS NULL))
  {extra}
ORDER BY r.created_at
"""


def resolve_slug(source_app: str, source_path: str) -> str | None:
    """Project slug for a stored session path under the current config."""
    from pipeline import ADAPTERS

    path = Path(source_path)
    for adapter in ADAPTERS:
        if adapter.app_name != source_app:
            continue
        try:
            return adapter.detect_project(path)
        except Exception:  # noqa: BLE001 - a missing/unreadable file means "unresolved"
            return None
    return None


def _chunk_flags(cur, raw_id: str) -> dict[str, str]:
    """{chunk_id: "flag,flag"} for the session's flagged chunks."""
    if not SCREENING_ENABLED:
        return {}
    cur.execute(
        "SELECT raw_content FROM devbrain.raw_sessions WHERE id = %s", (raw_id,)
    )
    row = cur.fetchone()
    messages = []
    try:
        doc = json.loads(row[0]) if row and row[0] else {}
        messages = [m for m in (doc.get("messages") or []) if isinstance(m, dict)]
    except (ValueError, AttributeError):
        messages = []
    cur.execute(
        "SELECT id::text, source_type, source_line_start, source_line_end, content "
        "FROM devbrain.chunks WHERE source_id = %s",
        (raw_id,),
    )
    rows = cur.fetchall()
    session_chunks = [
        SimpleNamespace(id=r[0], line_start=r[2], line_end=r[3])
        for r in rows
        if r[1] == "session" and r[2] is not None and r[3] is not None
    ]
    out: dict[str, str] = {}
    if messages and session_chunks:
        for c, flags in zip(
            session_chunks,
            screening.chunk_flags_from_messages(messages, session_chunks),
        ):
            if flags:
                out[c.id] = ",".join(flags)
    for cid, source_type, lo, hi, content in rows:
        if cid in out:
            continue
        # No structure to map (summary chunks, missing line ranges, non-JSON
        # transcript): screen the chunk text itself, untrusted.
        if source_type != "session" or lo is None or hi is None or not messages:
            flags = screening.flags_for(content)
            if flags:
                out[cid] = ",".join(flags)
    return out


def _has_flag_column(cur) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = 'devbrain' "
        "AND table_name = 'memory' AND column_name = 'screen_flags'"
    )
    return cur.fetchone() is not None


def repair_session(cur, raw_id: str, project_id: str) -> int:
    """Attach the project and write the missing memory rows on the caller's
    cursor (no commit). Returns the number of memory rows inserted."""
    cur.execute(
        "UPDATE devbrain.raw_sessions SET project_id = %s WHERE id = %s AND project_id IS NULL",
        (project_id, raw_id),
    )
    cur.execute(
        "UPDATE devbrain.chunks SET project_id = %s WHERE source_id = %s AND project_id IS NULL",
        (project_id, raw_id),
    )
    flags = _chunk_flags(cur, raw_id) if _has_flag_column(cur) else {}
    ids = list(flags)
    csvs = [flags[i] for i in ids]
    flag_col, flag_val, flag_join = "", "", ""
    params: list = [project_id]
    if ids:
        flag_col = ", screen_flags"
        flag_val = ", string_to_array(f.flags_csv, ',')"
        flag_join = "LEFT JOIN unnest(%s::uuid[], %s::text[]) AS f(id, flags_csv) ON f.id = c.id"
        params += [ids, csvs]
    params.append(raw_id)
    # The chunk's embedding is copied as stored: no model call, nothing
    # re-embedded. Chunk rows written by ingest's dual-write carry the
    # session id as provenance (migration 032) and dedupe on md5(content).
    cur.execute(
        f"""
        INSERT INTO devbrain.memory (project_id, kind, content, embedding, provenance_id{flag_col})
        SELECT %s, 'chunk', c.content, c.embedding, c.source_id{flag_val}
        FROM devbrain.chunks c
        {flag_join}
        WHERE c.source_id = %s AND c.content IS NOT NULL AND c.content <> ''
        ON CONFLICT (provenance_id, kind, md5(content))
            WHERE provenance_id IS NOT NULL AND kind = 'chunk' DO NOTHING
        """,
        params,
    )
    return cur.rowcount


def _plan_counts(cur, raw_id: str) -> tuple[int, int, int]:
    """(legacy chunks, chunks without a memory row, of those without an embedding)."""
    cur.execute(
        """
        SELECT count(*),
               count(*) FILTER (WHERE NOT EXISTS (
                   SELECT 1 FROM devbrain.memory m
                   WHERE m.provenance_id = c.source_id AND m.kind = 'chunk'
                     AND md5(m.content) = md5(c.content))),
               count(*) FILTER (WHERE c.embedding IS NULL AND NOT EXISTS (
                   SELECT 1 FROM devbrain.memory m
                   WHERE m.provenance_id = c.source_id AND m.kind = 'chunk'
                     AND md5(m.content) = md5(c.content)))
        FROM devbrain.chunks c WHERE c.source_id = %s
        """,
        (raw_id,),
    )
    a, b, n = cur.fetchone()
    return int(a), int(b), int(n)


def run(
    *,
    sessions: list[str] | None,
    apply: bool,
    rows_per_second: float,
    limit: int | None,
    out=sys.stdout,
) -> dict:
    from pipeline import is_excluded_path

    extra, params = "", []
    if sessions:
        extra = "AND (" + " OR ".join(["r.id::text LIKE %s"] * len(sessions)) + ")"
        params = [s.lower() + "%" for s in sessions]
    report = {
        "candidates": 0,
        "excluded": 0,
        "unresolved": 0,
        "resolved": 0,
        "chunks_missing_memory": 0,
        "chunks_missing_embedding": 0,
        "memory_rows_written": 0,
        "by_project": {},
        "unresolved_examples": [],
    }
    with get_connection() as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(_CANDIDATES_SQL.format(extra=extra), params)
            rows = cur.fetchall()
        conn.rollback()
        if limit:
            rows = rows[:limit]
        started = time.monotonic()
        written_total = 0
        for raw_id, app, source_path, current_project in rows:
            report["candidates"] += 1
            if is_excluded_path(Path(source_path)):
                report["excluded"] += 1
                continue
            slug = None
            project_id = current_project
            if not project_id:
                slug = resolve_slug(app, source_path)
                if not slug:
                    report["unresolved"] += 1
                    if len(report["unresolved_examples"]) < 20:
                        report["unresolved_examples"].append(source_path)
                    continue
            with conn.cursor() as cur:
                _, missing, no_emb = _plan_counts(cur, raw_id)
                if project_id and not slug:
                    cur.execute(
                        "SELECT slug FROM devbrain.projects WHERE id = %s",
                        (project_id,),
                    )
                    slug = cur.fetchone()[0]
            conn.rollback()
            report["resolved"] += 1
            report["chunks_missing_memory"] += missing
            report["chunks_missing_embedding"] += no_emb
            report["by_project"][slug] = report["by_project"].get(slug, 0) + 1
            if not apply:
                continue
            project_id = project_id or get_or_create_project_id(slug)
            with conn.cursor() as cur:
                n = repair_session(cur, raw_id, project_id)
            conn.commit()
            report["memory_rows_written"] += n
            written_total += n
            print(f"  {raw_id}: project {slug}, {n} memory row(s)", file=out)
            if rows_per_second > 0:
                # Throttle to the target insert rate.
                target = written_total / rows_per_second
                elapsed = time.monotonic() - started
                if target > elapsed:
                    time.sleep(target - elapsed)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sel = ap.add_mutually_exclusive_group(required=False)
    sel.add_argument(
        "--session",
        action="append",
        default=None,
        help="raw_sessions.id or an id prefix (repeatable)",
    )
    sel.add_argument("--all", action="store_true", help="every project-less session")
    ap.add_argument("--apply", action="store_true", help="write (default: report only)")
    ap.add_argument(
        "--rows-per-second",
        type=float,
        default=300.0,
        help="memory insert rate cap (0 = no throttle)",
    )
    ap.add_argument("--limit", type=int, default=None, help="at most N sessions")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args(argv)
    if args.apply and not (args.session or args.all):
        ap.error("--apply needs --session or --all")
    report = run(
        sessions=args.session,
        apply=args.apply,
        rows_per_second=args.rows_per_second,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for k, v in report.items():
            print(f"{k}: {v}")
        if not args.apply:
            rate = args.rows_per_second or 300.0
            print(
                f"estimated --apply time at {rate:g} rows/s: "
                f"{report['chunks_missing_memory'] / rate / 60:.1f} min"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
