"""cognify-reextract CLI — on-demand re-extraction for specific sessions.

Provides the ``devbrain cognify-reextract`` command:

    devbrain cognify-reextract --session=<id>      # re-extract one session
    devbrain cognify-reextract --all               # re-extract all sessions
    devbrain cognify-reextract --since=<date>      # sessions ingested after date
    devbrain cognify-reextract --since-version=N   # sessions with extraction_version < N

Re-extraction archives prior lessons/decisions (sets archived_at; never
deletes — HIPAA audit trail), then runs extract_from_session with
reextract=True. New rows carry applies_when.reextracted_from = <prior_row_id>
for traceability (per Phase 6 design §6). New rows also receive
extraction_version = CURRENT_EXTRACTION_VERSION from extract.py.

--since-version=N: selects all sessions that have at least one tier='lesson'
row with extraction_version < N. Useful when bumping CURRENT_EXTRACTION_VERSION
to reprocess rows produced by an older extraction prompt or model.

This module only contains the CLI logic and ``run_reextract``; the actual
re-extraction is delegated to cognify.extract.extract_from_session.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def run_reextract(
    conn: Any,
    project_id: Any,
    *,
    session_id: str | None = None,
    all_sessions: bool = False,
    since: str | None = None,
    since_version: int | None = None,
    dry_run: bool = False,
) -> dict:
    """Re-extract lessons/decisions for the specified sessions.

    Exactly one of session_id / all_sessions / since / since_version must
    be set.

    Args:
        conn: psycopg2 connection.
        project_id: UUID. Required.
        session_id: provenance_id of the specific session to re-extract.
        all_sessions: if True, re-extract all sessions for the project.
        since: ISO date string; re-extract sessions ingested after this date.
        since_version: integer N; re-extract sessions that have at least one
            tier='lesson' row with extraction_version < N. Old rows gain
            archived_at = NOW() and applies_when.reextracted_from = <prior_id>.
            New rows are written with CURRENT_EXTRACTION_VERSION.
        dry_run: compute what would happen without mutating.

    Returns:
        dict with summary counts.
    """
    from cognify.extract import extract_from_session, _sessions_since

    flags_set = sum(
        [bool(session_id), all_sessions, bool(since), since_version is not None]
    )
    if flags_set != 1:
        raise ValueError(
            "Exactly one of --session, --all, --since, or --since-version "
            "must be specified."
        )

    # Resolve target sessions.
    if session_id:
        sessions = [session_id]
    elif all_sessions:
        sessions = _sessions_since(conn, project_id, since=None)
    elif since is not None:
        # Parse since date.
        try:
            since_dt = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise ValueError(f"Invalid --since date: {since!r}") from exc
        sessions = _sessions_since(conn, project_id, since=since_dt)
    else:
        # since_version: find sessions that have lesson rows with old version.
        sessions = _sessions_below_version(conn, project_id, since_version)

    if not sessions:
        return {
            "sessions_targeted": 0,
            "lessons_created": 0,
            "decisions_created": 0,
        }

    if dry_run:
        return {
            "dry_run": True,
            "sessions_targeted": len(sessions),
            "sessions": sessions,
        }

    total_lessons = 0
    total_decisions = 0

    for sid in sessions:
        result = extract_from_session(
            conn,
            sid,
            project_id,
            reextract=True,
        )
        total_lessons += result.lessons_created
        total_decisions += result.decisions_created
        # archived count is recorded internally via _archive_prior_extracts.

    return {
        "sessions_targeted": len(sessions),
        "lessons_created": total_lessons,
        "decisions_created": total_decisions,
    }


def _sessions_below_version(
    conn: Any, project_id: Any, version: int
) -> list[str]:
    """Return session IDs (as strings) that have tier='lesson' rows with
    extraction_version < version.

    A session is identified by applies_when->>'source_session'. Rows without
    a source_session are excluded (they predate the applies_when schema).

    Returns a deduplicated, ordered list suitable for passing to
    extract_from_session.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT applies_when->>'source_session' AS session_id "
            "FROM devbrain.memory "
            "WHERE project_id = %s "
            "  AND tier = 'lesson' "
            "  AND archived_at IS NULL "
            "  AND applies_when->>'source_session' IS NOT NULL "
            "  AND extraction_version < %s "
            "ORDER BY session_id",
            (project_id, version),
        )
        return [row[0] for row in cur.fetchall()]
