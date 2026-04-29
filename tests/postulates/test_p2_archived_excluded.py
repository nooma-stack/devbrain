"""P2 — Archived memory is excluded from the curator brief.

POSTULATE
---------
A memory with archived_at IS NOT NULL must never appear in the
curator's project brief, regardless of its strength or recency.

STATUS
------
xfail(strict=True) until the curator agent (Atlas Step 5 in
docs/plans/2026-04-29-phase-3-discipline-layer.md §4) ships. The
substrate (archived_at column on devbrain.memory) is already in
place from migration 010, so the *data* the curator will need to
read is fully testable today — only the curator function itself is
missing.

When Step 5 lands this xfail flips XPASS and CI forces us back here
to remove the marker.
"""
from __future__ import annotations

import pytest


@pytest.mark.xfail(
    strict=True,
    reason="Curator agent + brief assembly lands in Atlas Step 5; "
    "see docs/plans/2026-04-29-phase-3-discipline-layer.md §4.",
)
def test_archived_memory_not_in_curator_brief(
    conn, project_factory, memory_factory
):
    project = project_factory("p2")
    live = memory_factory(
        project["id"], kind="pattern", content="live pattern: prefer asyncpg"
    )
    stale = memory_factory(
        project["id"], kind="pattern", content="stale pattern: prefer aiopg"
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET archived_at = now() WHERE id = %s",
            (stale["id"],),
        )
    conn.commit()

    # Curator entrypoint does not exist yet. Once Atlas Step 5 lands,
    # replace this with the real `assemble_curator_brief(project_id)`
    # call. Until then we import-fail and pytest treats the test as
    # xfail.
    from factory.curator import assemble_curator_brief  # type: ignore[import-not-found]

    brief = assemble_curator_brief(project["id"])
    brief_ids = {entry.memory_id for entry in brief.entries}

    assert live["id"] in brief_ids
    assert stale["id"] not in brief_ids
