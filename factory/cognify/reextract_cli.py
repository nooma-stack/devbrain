"""cognify-reextract CLI — on-demand re-extraction for specific sessions.

Provides the ``devbrain cognify-reextract`` command:

    devbrain cognify-reextract --session=<id>   # re-extract one session
    devbrain cognify-reextract --all            # re-extract all sessions in project
    devbrain cognify-reextract --since=<date>   # sessions ingested after date

Re-extraction archives prior lessons/decisions (sets archived_at; never
deletes — HIPAA audit trail), then runs extract_from_session with
reextract=True. New rows carry metadata.reextracted_from = <prior_row_id>
for traceability.

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
    dry_run: bool = False,
) -> dict:
    """Re-extract lessons/decisions for the specified sessions.

    Exactly one of session_id / all_sessions / since must be set.

    Args:
        conn: psycopg2 connection.
        project_id: UUID. Required.
        session_id: provenance_id of the specific session to re-extract.
        all_sessions: if True, re-extract all sessions for the project.
        since: ISO date string; re-extract sessions ingested after this date.
        dry_run: compute what would happen without mutating.

    Returns:
        dict with summary counts.
    """
    from cognify.extract import extract_from_session, _sessions_since

    if sum([bool(session_id), all_sessions, bool(since)]) != 1:
        raise ValueError(
            "Exactly one of --session, --all, or --since must be specified."
        )

    # Resolve target sessions.
    if session_id:
        sessions = [session_id]
    elif all_sessions:
        sessions = _sessions_since(conn, project_id, since=None)
    else:
        # Parse since date.
        try:
            since_dt = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise ValueError(f"Invalid --since date: {since!r}") from exc
        sessions = _sessions_since(conn, project_id, since=since_dt)

    if not sessions:
        return {
            "sessions_targeted": 0,
            "lessons_created": 0,
            "decisions_created": 0,
            "archived": 0,
        }

    if dry_run:
        return {
            "dry_run": True,
            "sessions_targeted": len(sessions),
            "sessions": sessions,
        }

    total_lessons = 0
    total_decisions = 0
    total_archived = 0

    for sid in sessions:
        result = extract_from_session(
            conn,
            sid,
            project_id,
            reextract=True,
        )
        total_lessons += result.lessons_created
        total_decisions += result.decisions_created
        # archived count is not returned by extract_from_session directly;
        # it's recorded internally via _archive_prior_extracts.

    return {
        "sessions_targeted": len(sessions),
        "lessons_created": total_lessons,
        "decisions_created": total_decisions,
    }
