"""cognify.refine — refinement pipeline (moved from factory/curator/refinement.py).

This module is the cognify home for applies_when widening. The logic is
identical to factory/curator/refinement.py — this is a pure relocation.
factory/curator/refinement.py now imports from here and re-exports the
public API for backwards compatibility with existing callers.

Public API (unchanged from refinement.py):
  queue_refinement(conn, finding)
  refine_applies_when(conn, project_id)

See factory/curator/refinement.py docstring for full design notes.
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
            no-op — heuristic findings don't have a specific memory row to widen.

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

    Stale rows (queued_at older than 7 days) are skipped.
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
    """Merge (file_pattern, keywords) into a memory's applies_when JSONB."""
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
    """Convert a specific file path to a directory-level glob."""
    if not path or "/" not in path:
        return path or ""
    parts = path.rsplit("/", 1)
    if len(parts) != 2:
        return path
    dir_part, _filename = parts
    return f"{dir_part}/*.py"


def _extract_keywords(text: str) -> list[str]:
    """Naive keyword extraction: words >= 4 chars, deduped, capped at 5."""
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
