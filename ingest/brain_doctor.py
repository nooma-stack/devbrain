#!/usr/bin/env python3 -u
"""Routine health checks for the devbrain memory system ("brain rot" detector).

Every check here is a mechanized version of a failure we actually hit and
originally caught by ACCIDENT (2026-08-24 → 09-01):

  embedding_drift   — ollama upgrade shifted arctic-embed2's numerics; old
                      corpus ranked ~1% off-space (caught via a missing
                      checkpoint, not monitoring)
  ann_self_recall   — REINDEX CONCURRENTLY corrupted the HNSW graph; rows
                      became unreachable by index scans while by-id reads
                      worked (caught the same accidental way)
  index_valid       — the cheap structural cousin of the above
  junk_summaries    — a blank-input bug paid a model to summarize nothing,
                      741× (caught because the BILL looked suspiciously low)
  ingest_liveness   — a dead watcher silently stops all new memory
  cognify_liveness  — passes stall or error-loop (codex failure storm burned
                      62K fallbacks before anyone looked)
  orphan_backlog    — unclosed sessions accumulating again
  summary_coverage  — sessions ingested but never summarized
  worker_quota      — closure worker hitting rate limits / exhausted credits

Output: one line per check (PASS/WARN/FAIL + detail) to stdout and appended
to logs/brain-doctor.log. Any FAIL also inserts a devbrain.notifications row
(event_type='health_check_failed') so agent sessions and dashboards see it.
Exit code = number of FAILs. Run daily via launchd (template alongside).
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import psycopg2

from config import DATABASE_URL, DEVBRAIN_HOME, EMBED_MODEL, OLLAMA_URL

LOG_PATH = Path(DEVBRAIN_HOME) / "logs" / "brain-doctor.log"

JUNK_SQL = """(content ILIKE '%cannot be determined%' OR content ILIKE '%transcript%empty%'
    OR content ILIKE '%no transcript content%' OR content ILIKE '%no actionable work%'
    OR content ILIKE '%no session data%' OR content ILIKE '%without a recoverable%')"""


def _embed(text: str) -> list[float]:
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed",
        data=json.dumps({"model": EMBED_MODEL, "input": text}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["embeddings"][0]


def check_embedding_drift(cur):
    """Stored-vs-recomputed cosine must be ~1.0 for old AND new rows."""
    cur.execute("""(SELECT id, coalesce(title,''), content, kind FROM devbrain.memory
        WHERE embedding IS NOT NULL AND kind IN ('decision','pattern')
        ORDER BY created_at ASC LIMIT 4)
        UNION ALL
        (SELECT id, coalesce(title,''), content, kind FROM devbrain.memory
        WHERE embedding IS NOT NULL AND kind IN ('decision','pattern')
        ORDER BY created_at DESC LIMIT 4)""")
    worst = 1.0
    for mid, title, content, kind in cur.fetchall():
        text = f"{title}\n{content}" if title else content
        vs = "[" + ",".join(f"{x:.6f}" for x in _embed(text)) + "]"
        cur.execute("SELECT 1-(embedding <=> %s::vector) FROM devbrain.memory WHERE id=%s",
                    (vs, mid))
        worst = min(worst, float(cur.fetchone()[0]))
    if worst < 0.999:
        return "FAIL", (f"stored-vs-recomputed cosine {worst:.5f} < 0.999 — "
                        "embedding runtime drifted; corpus re-embed required "
                        "(see handoff doc §2/§4)")
    return "PASS", f"worst stored-vs-recomputed sim {worst:.5f}"


def check_ann_self_recall(cur):
    """Every row must find ITSELF via an index scan — unreachable nodes
    mean a corrupted HNSW graph (the REINDEX CONCURRENTLY disease)."""
    cur.execute("""SELECT id FROM devbrain.memory
        WHERE embedding IS NOT NULL AND archived_at IS NULL
        AND created_at > now() - interval '60 days'""")
    ids = [r[0] for r in cur.fetchall()]
    if len(ids) < 5:
        return "WARN", "too few recent rows to sample"
    sample = random.sample(ids, min(20, len(ids)))
    cur.execute("SET hnsw.ef_search = 200")
    misses = 0
    for mid in sample:
        cur.execute("""SELECT count(*) FROM (
            SELECT m2.id FROM devbrain.memory m2
            WHERE m2.embedding IS NOT NULL AND m2.archived_at IS NULL
            ORDER BY m2.embedding <=> (SELECT embedding FROM devbrain.memory WHERE id=%s)
            LIMIT 10) t WHERE t.id = %s""", (mid, mid))
        if not cur.fetchone()[0]:
            misses += 1
    if misses > 1:
        return "FAIL", (f"{misses}/{len(sample)} rows cannot find THEMSELVES via "
                        "the index — HNSW graph is degraded; rebuild SERIALLY "
                        "(never REINDEX CONCURRENTLY)")
    return "PASS", f"self-recall {len(sample)-misses}/{len(sample)}"


def check_index_valid(cur):
    cur.execute("""SELECT c.relname, i.indisvalid, i.indisready FROM pg_index i
        JOIN pg_class c ON c.oid=i.indexrelid
        WHERE i.indrelid IN ('devbrain.memory'::regclass, 'devbrain.chunks'::regclass)
        AND pg_get_indexdef(i.indexrelid) ILIKE '%hnsw%'""")
    bad = [r[0] for r in cur.fetchall() if not (r[1] and r[2])]
    if bad:
        return "FAIL", f"invalid/not-ready vector indexes: {bad}"
    return "PASS", "all vector indexes valid"


def check_junk_summaries(cur):
    cur.execute(f"""SELECT count(*), count(*) FILTER (WHERE {JUNK_SQL})
        FROM devbrain.memory WHERE kind='session_summary' AND archived_at IS NULL
        AND created_at > now() - interval '7 days'""")
    total, junk = cur.fetchone()
    if total and junk / total > 0.10:
        return "FAIL", (f"{junk}/{total} recent summaries match junk patterns "
                        "(>10%) — a summarizer is being fed blank/broken input. "
                        "Audit per handoff doc §11 (sample before archiving; "
                        "patterns have false positives)")
    return "PASS", f"junk rate {junk}/{total or 0} in 7d"


def check_ingest_liveness(_cur):
    log = Path(DEVBRAIN_HOME) / "logs" / "ingest.log"
    if not log.exists():
        return "WARN", "ingest.log missing"
    age_h = (time.time() - log.stat().st_mtime) / 3600
    if age_h > 3:
        return "FAIL", f"ingest watcher silent for {age_h:.1f}h — new sessions are not becoming memory"
    return "PASS", f"ingest active {age_h:.1f}h ago"


def check_cognify_liveness(cur):
    problems = []
    for pass_name, max_gap_h in (("extract", 4), ("resummarize", 4),
                                 ("fanout", 4), ("edges", 26)):
        cur.execute("""SELECT max(started_at) FROM devbrain.cognify_run_log
            WHERE pass_name=%s AND completed_at IS NOT NULL""", (pass_name,))
        last = cur.fetchone()[0]
        if last is None or (datetime.now(timezone.utc) - last).total_seconds() > max_gap_h * 3600:
            problems.append(f"{pass_name} last completed {last}")
    cur.execute("""SELECT count(*) FROM devbrain.cognify_run_log
        WHERE started_at > now() - interval '24 hours' AND error IS NOT NULL""")
    errs = cur.fetchone()[0]
    if errs:
        problems.append(f"{errs} errored runs in 24h")
    if problems:
        return "FAIL", "; ".join(problems)
    return "PASS", "all passes on cadence, no errors 24h"


def check_orphan_backlog(_cur):
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "close_orphan_sessions.py"),
         "--report"], capture_output=True, text=True, timeout=1800)
    line = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
    try:
        unclosed = int(line.split(" unclosed")[0].rsplit(" ", 1)[-1].replace(",", ""))
    except (ValueError, IndexError):
        return "WARN", f"could not parse scanner output: {line[:120]}"
    if unclosed > 25:
        return "FAIL", (f"{unclosed} unclosed settled sessions — closure "
                        "pipeline (hook/scanner) is falling behind")
    return "PASS", f"{unclosed} unclosed ({line[:90]})"


def check_summary_coverage(cur):
    cur.execute("""SELECT count(*) FROM devbrain.raw_sessions
        WHERE summary IS NULL AND created_at < now() - interval '36 hours'
        AND created_at > now() - interval '14 days'""")
    n = cur.fetchone()[0]
    if n > 10:
        return "FAIL", f"{n} sessions ingested >36h ago still have no summary"
    return "PASS", f"{n} unsummarized aged sessions"


def check_worker_quota(_cur):
    logs = sorted((Path(DEVBRAIN_HOME) / "logs").glob("closure-*.log"),
                  key=lambda p: p.stat().st_mtime)
    if not logs:
        return "PASS", "no closure logs yet"
    tail = logs[-1].read_text()[-4000:]
    if "insufficient_quota" in tail or "credit_balance_exhausted" in tail:
        return "FAIL", "closure worker hit EXHAUSTED CREDITS — add credits (waiting will not help)"
    if "ABORTING RUN" in tail:
        return "WARN", "closure worker aborted on persistent 429 recently"
    return "PASS", f"latest worker log clean ({logs[-1].name})"


CHECKS = [
    ("embedding_drift", check_embedding_drift),
    ("ann_self_recall", check_ann_self_recall),
    ("index_valid", check_index_valid),
    ("junk_summaries", check_junk_summaries),
    ("ingest_liveness", check_ingest_liveness),
    ("cognify_liveness", check_cognify_liveness),
    ("orphan_backlog", check_orphan_backlog),
    ("summary_coverage", check_summary_coverage),
    ("worker_quota", check_worker_quota),
]


def main() -> int:
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    lines, fails = [f"=== brain doctor {stamp} ==="], []
    for name, fn in CHECKS:
        try:
            status, detail = fn(cur)
        except Exception as exc:
            status, detail = "FAIL", f"check crashed: {str(exc)[:150]}"
        lines.append(f"{status:<5} {name}: {detail}")
        if status == "FAIL":
            fails.append(f"{name}: {detail}")
    report = "\n".join(lines)
    print(report, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as fh:
        fh.write(report + "\n")
    if fails:
        cur.execute(
            """INSERT INTO devbrain.notifications
               (recipient_dev_id, event_type, title, body, channels_attempted,
                channels_delivered, sent_at)
               VALUES (NULL, 'health_check_failed', %s, %s, '{log}', '{log}', now())""",
            (f"brain doctor: {len(fails)} check(s) FAILING",
             "\n".join(fails)))
    return len(fails)


if __name__ == "__main__":
    sys.exit(main())
