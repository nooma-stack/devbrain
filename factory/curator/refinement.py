"""Curator self-introspection — proposes applies_when widening for memories
that should have been in the brief but weren't (signal #2).

Lifecycle:
  1. apply_feedback_signals (graduation.py) sees a finding whose
     relevant_memory_id is NOT in the brief and calls queue_refinement.
  2. queue_refinement extracts a file_pattern + keywords from the
     finding's file/message and writes a row into devbrain.refinement_queue.
  3. At the end of every REVIEWING phase, refine_applies_when dequeues
     pending rows for the project and merges (file_pattern, keywords)
     into each memory's applies_when JSONB.

Design notes:
  - 7-day stale window: queue rows older than 7 days are skipped but
    still get applied_at set so they don't pile up forever and so a
    re-tick of refine_applies_when doesn't keep re-considering them.
  - Error-path persistence: if _widen_applies_when raises, the row gets
    applied_at = NOW() AND error = <msg>. We don't retry indefinitely.
  - Cross-project safety: the dequeue JOINs memory and filters on
    project_id so a refinement queued in project A can't be applied to
    a memory row in project B even if the queue table was tampered with.
  - applies_when shape: existing values like {"category": "tools"} are
    preserved; we only merge "files" and "keywords" array keys.

v3.0: simple keyword extraction from finding.file + finding.message.
Phase 3.x: smarter heuristic or LLM-driven proposal.

Public API:
- queue_refinement(conn, finding)
- refine_applies_when(conn, project_id)
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)


def queue_refinement(conn, finding):
    """Queue a signal-#2 case for end-of-tick refinement.

    Args:
        conn: psycopg2 connection. Commit happens inside.
        finding: an EvalFinding. If relevant_memory_id is None this is a
            no-op — heuristic findings (rule_id is None) don't have a
            specific memory row to widen.

    Side effects:
        Writes one row into devbrain.refinement_queue with file_pattern
        derived from finding.file (directory glob) and keywords extracted
        from finding.message.
    """
    if finding.relevant_memory_id is None:
        return

    file_pattern = _file_glob_from_path(finding.file)
    keywords = _extract_keywords(finding.message)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.refinement_queue "
            "(memory_id, file_pattern, keywords) VALUES (%s, %s, %s)",
            (finding.relevant_memory_id, file_pattern, keywords),
        )
    conn.commit()


def refine_applies_when(conn, project_id):
    """Process queued refinements: widen each memory's applies_when.

    Args:
        conn: psycopg2 connection. Commit happens at the end.
        project_id: UUID. Only queue rows whose memory belongs to this
            project are processed (cross-project safety).

    Per row:
      - On success: applied_at = NOW().
      - On _widen_applies_when raising: applied_at = NOW(), error = msg
        (truncated to 500 chars). The row is NOT retried.

    Stale rows (queued_at older than 7 days) are skipped entirely — they
    don't get applied_at set on this pass, but they're filtered out by
    the queued_at > NOW() - INTERVAL '7 days' predicate so they won't be
    considered again by future ticks either.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT q.id, q.memory_id, q.file_pattern, q.keywords
            FROM devbrain.refinement_queue q
            JOIN devbrain.memory m ON m.id = q.memory_id
            WHERE q.applied_at IS NULL
              AND q.queued_at > NOW() - INTERVAL '7 days'
              AND m.project_id = %s
            """,
            (project_id,),
        )
        rows = cur.fetchall()

        for queue_id, memory_id, file_pattern, keywords in rows:
            try:
                _widen_applies_when(conn, memory_id, file_pattern, keywords)
                cur.execute(
                    "UPDATE devbrain.refinement_queue "
                    "SET applied_at = NOW() WHERE id = %s",
                    (queue_id,),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "refine_applies_when: queue row %s failed", queue_id
                )
                cur.execute(
                    "UPDATE devbrain.refinement_queue "
                    "SET applied_at = NOW(), error = %s WHERE id = %s",
                    (str(exc)[:500], queue_id),
                )
    conn.commit()


def _widen_applies_when(conn, memory_id, file_pattern, keywords):
    """Merge (file_pattern, keywords) into a memory's applies_when JSONB.

    Behavior:
      - NULL applies_when is treated as {} (no existing keys to preserve).
      - Existing keys (e.g. {"category": "tools"}) are preserved verbatim.
      - "files" and "keywords" array keys are merged with the new values
        and deduplicated. Sorted for determinism.
      - If the memory row no longer exists (e.g. archived), this is a
        no-op (the FK CASCADE on the queue would have already wiped the
        queue row in normal flow, but defensive code can't hurt).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT applies_when FROM devbrain.memory WHERE id = %s",
            (memory_id,),
        )
        row = cur.fetchone()
        if row is None:
            return
        current = row[0] or {}
        files = set(current.get("files", []))
        if file_pattern:
            files.add(file_pattern)
        kw_set = set(current.get("keywords", []))
        kw_set.update(keywords or [])

        new_aw = {**current, "files": sorted(files), "keywords": sorted(kw_set)}
        cur.execute(
            "UPDATE devbrain.memory SET applies_when = %s::jsonb WHERE id = %s",
            (json.dumps(new_aw), memory_id),
        )


def _file_glob_from_path(path: str) -> str:
    """Convert a specific file path to a directory-level glob.

    Examples:
      'factory/curator/worker.py' -> 'factory/curator/*.py'
      'README.md'                 -> 'README.md'  (no slash, returned as-is)
      ''                          -> ''
      None                        -> ''

    The glob granularity is intentionally coarse — refinement is meant to
    widen, not narrow.
    """
    if not path or "/" not in path:
        return path or ""
    parts = path.rsplit("/", 1)
    if len(parts) != 2:
        return path
    dir_part, _filename = parts
    return f"{dir_part}/*.py"


def _extract_keywords(text: str) -> list[str]:
    """Naive keyword extraction: words >= 4 chars, deduped, capped at 5.

    Lower-cases the input. Order is preserved (first occurrence wins).
    Cap of 5 keeps the resulting JSONB array bounded — applies_when is
    read on every brief generation so we shouldn't bloat it unboundedly.
    """
    if not text:
        return []
    words = re.findall(r"\b[a-zA-Z_]{4,}\b", text.lower())
    seen = set()
    keywords: list[str] = []
    for w in words:
        if w not in seen:
            seen.add(w)
            keywords.append(w)
        if len(keywords) >= 5:
            break
    return keywords
