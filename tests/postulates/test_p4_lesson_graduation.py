"""P4 — Lesson graduation.

POSTULATE
---------
A tier='lesson' row that:
  1. Receives 3 successful preventions (signal #3) consecutively
  2. Has last_hit within the 90-day freshness window

...transitions to tier='rule', graduated_at is set, and a memory_ledger
row records the transition (via the AFTER trigger from Step 2 substrate).

This is the verification gate for Phase 6c. It's the end-to-end contract
that the three-signal feedback loop must honor: signal #3 fires, the
streak counter advances, and at the threshold the tier flips with a
ledger audit row written by the migration-015 trigger.

STATUS
------
Activated in Atlas Step 6c — the graduation pipeline lands here. Phase
6a shipped the schema (current_streak + graduated_at columns + the
graduation-candidates index). Phase 6b shipped the eval substrate.
Phase 6c wires it together: the three signal handlers in
factory/curator/graduation.py walk the brief + eval results and call
_signal_success which advances the streak and graduates at threshold.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The postulate suite runs from repo root; the curator module lives under
# factory/. Add factory/ to sys.path so the import resolves regardless of
# where pytest is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "factory"))

from curator.graduation import _signal_success  # noqa: E402


def test_lesson_graduates_after_3_consecutive_successes(
    conn, project_factory, memory_factory
):
    project = project_factory("p4_grad")
    lesson = memory_factory(
        project["id"], kind="pattern", content="will graduate"
    )
    # The factory inserts at tier='memory' by default; bump to 'lesson'
    # so the graduation gate applies. Pre-seed current_streak = 2 — one
    # more success crosses the threshold.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory "
            "SET tier = 'lesson', current_streak = 2 "
            "WHERE id = %s",
            (lesson["id"],),
        )
    conn.commit()

    # Capture the ledger seq before the signal fires so we can assert
    # the graduation transition produced a NEW ledger row.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory_ledger WHERE memory_id = %s",
            (lesson["id"],),
        )
        ledger_count_before = cur.fetchone()[0]

    _signal_success(conn, lesson["id"])

    # Verify tier flipped, graduated_at set, streak landed at threshold.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tier, graduated_at, current_streak FROM devbrain.memory "
            "WHERE id = %s",
            (lesson["id"],),
        )
        tier, graduated_at, streak = cur.fetchone()
    assert tier == "rule"
    assert graduated_at is not None
    assert streak == 3

    # The AFTER UPDATE trigger from migration 015 writes a ledger row on
    # every UPDATE. The signal-success UPDATE writes one, the graduation
    # UPDATE writes a second — the postulate just asserts the ledger
    # advanced (>= 1 new row), not the exact count.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory_ledger WHERE memory_id = %s",
            (lesson["id"],),
        )
        ledger_count_after = cur.fetchone()[0]
    assert ledger_count_after > ledger_count_before
