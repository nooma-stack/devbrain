"""P5 — Rule demotion.

POSTULATE
---------
A tier='rule' row whose effective_hit_count / (hit_count + effective_hit_count)
< 0.50 within a 30-day window transitions to tier='lesson', demoted_at is
set, current_streak is reset to 0, and a memory_ledger row records the
transition (via the AFTER trigger from Step 2 substrate).

This is the verification gate for Phase 6e. It's the end-to-end contract
that the rule-demotion sweep must honor: a rule that has been recently
active but is misfiring (precision below 0.5 over a 30-day window) is
demoted back to tier='lesson' so it has to re-earn graduation through
the three-signal feedback loop.

STATUS
------
Activated in Atlas Step 6e — the state machine integration phase. The
sweep itself ships in factory/curator/graduation.py
(demote_low_precision_rules) which is invoked from the IMPLEMENTING ->
REVIEWING hook in factory/state_machine.py._run_eval_phase.
"""
from __future__ import annotations

import sys
from pathlib import Path

# The postulate suite runs from repo root; the curator module lives under
# factory/. Add factory/ to sys.path so the import resolves regardless of
# where pytest is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "factory"))

from curator.graduation import demote_low_precision_rules  # noqa: E402


def test_low_precision_rule_demotes_to_lesson(
    conn, project_factory, memory_factory
):
    project = project_factory("p5_demote")
    rule = memory_factory(project["id"], content="will demote")

    # Pre-seed: hit_count=5 + effective_hit_count=3 -> precision=3/8=0.375 < 0.5
    # current_streak=10 (will reset to 0 on demotion)
    # last_hit=NOW (within 30-day window)
    # The postulate suite's memory_factory inserts at tier='memory' with
    # zero counters; bump tier='rule' + counters in one UPDATE.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory "
            "SET tier = 'rule', hit_count = 5, effective_hit_count = 3, "
            "    current_streak = 10, last_hit = NOW() "
            "WHERE id = %s",
            (rule["id"],),
        )
    conn.commit()

    # Capture the ledger seq before the demotion fires so we can assert
    # the tier transition produced a NEW ledger row.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory_ledger WHERE memory_id = %s",
            (rule["id"],),
        )
        ledger_count_before = cur.fetchone()[0]

    demote_low_precision_rules(conn, project["id"])

    # Verify tier flipped, demoted_at set, streak reset.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tier, demoted_at, current_streak FROM devbrain.memory "
            "WHERE id = %s",
            (rule["id"],),
        )
        tier, demoted_at, streak = cur.fetchone()
    assert tier == "lesson"
    assert demoted_at is not None
    assert streak == 0

    # The AFTER UPDATE trigger from migration 015 writes a ledger row on
    # every UPDATE. The demotion UPDATE writes one — the postulate just
    # asserts the ledger advanced (>= 1 new row), not the exact count.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory_ledger WHERE memory_id = %s",
            (rule["id"],),
        )
        ledger_count_after = cur.fetchone()[0]
    assert ledger_count_after > ledger_count_before
