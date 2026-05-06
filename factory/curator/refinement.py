"""Curator self-introspection — proposes applies_when widening.

MOVED TO factory/cognify/refine.py (Atlas Phase 6c).

This module is now a thin backwards-compatibility shim. All logic lives in
``cognify.refine``. Existing callers continue to import from
``curator.refinement`` unchanged (including private helpers used in tests).

Public API (re-exported from cognify.refine — behaviour unchanged):
- queue_refinement(conn, finding)
- refine_applies_when(conn, project_id)
- _extract_keywords
- _file_glob_from_path
- _widen_applies_when

Implementation note: ``refine_applies_when`` is re-implemented here as a
thin wrapper rather than a bare re-export so that monkey-patching
``curator.refinement._widen_applies_when`` in tests continues to work.
The shim's ``refine_applies_when`` calls ``_widen_applies_when`` through
this module's own namespace, which is the attribute tests patch.
"""
from __future__ import annotations

import logging

from cognify.refine import (  # noqa: F401
    _extract_keywords,
    _file_glob_from_path,
    _widen_applies_when,
    queue_refinement,
)

logger = logging.getLogger(__name__)


def refine_applies_when(conn, project_id):
    """Process queued refinements: widen each memory's applies_when.

    Thin wrapper that delegates the dequeue query to cognify.refine but calls
    _widen_applies_when through *this module's* namespace so monkey-patching
    ``curator.refinement._widen_applies_when`` in tests works correctly.
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
