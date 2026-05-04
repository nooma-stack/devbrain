"""P2 — Archived memory is excluded from the curator brief.

POSTULATE
---------
A memory with archived_at IS NOT NULL must never appear in the
curator's project brief, regardless of its strength or recency.

Activated in Atlas Step 5d. Was xfail(strict=True) until the curator
agent landed. See docs/plans/2026-05-04-step-5-curator-design.md §3.
The substrate (archived_at column) ships in migration 010; the
filter discipline lives in factory/curator/brief.py — every
SELECT off devbrain.memory contains `archived_at IS NULL`.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

# Make `from curator.brief import ...` resolve from tests/postulates/.
_FACTORY = Path(__file__).resolve().parents[2] / "factory"
if str(_FACTORY) not in sys.path:
    sys.path.insert(0, str(_FACTORY))

from curator.brief import generate_brief  # noqa: E402


def test_archived_memory_not_in_curator_brief(
    conn, project_factory, memory_factory
):
    project = project_factory("p2")

    # Live memory + lesson + rule — every section the brief surfaces.
    live_decision = memory_factory(
        project["id"], kind="pattern",
        content="live pattern: prefer asyncpg",
    )
    stale_decision = memory_factory(
        project["id"], kind="pattern",
        content="stale pattern: prefer aiopg",
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET archived_at = now() WHERE id = %s",
            (stale_decision["id"],),
        )

        # Insert a lesson + rule with one archived sibling each so the
        # postulate covers all three sections of the brief.
        cur.execute(
            "INSERT INTO devbrain.memory "
            "(project_id, kind, title, content, tier) "
            "VALUES (%s, 'pattern', 'live-lesson', 'live lesson', 'lesson') "
            "RETURNING id",
            (project["id"],),
        )
        live_lesson_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO devbrain.memory "
            "(project_id, kind, title, content, tier, archived_at) "
            "VALUES (%s, 'pattern', 'stale-lesson', 'stale lesson', "
            "        'lesson', now()) "
            "RETURNING id",
            (project["id"],),
        )
        stale_lesson_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO devbrain.memory "
            "(project_id, kind, title, content, tier) "
            "VALUES (%s, 'decision', 'live-rule', 'live rule', 'rule') "
            "RETURNING id",
            (project["id"],),
        )
        live_rule_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO devbrain.memory "
            "(project_id, kind, title, content, tier, archived_at) "
            "VALUES (%s, 'decision', 'stale-rule', 'stale rule', 'rule', "
            "        now()) "
            "RETURNING id",
            (project["id"],),
        )
        stale_rule_id = cur.fetchone()[0]

        # The brief lookup is keyed on factory_jobs.id (cascade target
        # for the persist UPDATE). Use a real factory_jobs row so the
        # UPDATE inside generate_brief succeeds — without a row the
        # silent UPDATE 0-rows path would still let the test pass on
        # the SELECT side, but a missing factory_jobs row also makes
        # the contract clearer if generate_brief ever grows a returning-
        # rowcount assertion.
        cur.execute(
            "INSERT INTO devbrain.factory_jobs "
            "(project_id, title, spec, status) "
            "VALUES (%s, 'p2-test', 'asyncpg pattern', 'queued') "
            "RETURNING id",
            (project["id"],),
        )
        job_id = cur.fetchone()[0]
    conn.commit()

    try:
        brief = generate_brief(conn, job_id, project["id"], "asyncpg pattern")

        decision_ids = {r.id for r in brief.relevant_decisions}
        lesson_ids = {r.id for r in brief.lessons}
        rule_ids = {r.id for r in brief.rules}

        # Live entries surface in their respective sections.
        assert live_decision["id"] in decision_ids
        assert live_lesson_id in lesson_ids
        assert live_rule_id in rule_ids

        # Archived entries never surface, regardless of section.
        assert stale_decision["id"] not in decision_ids
        assert stale_lesson_id not in lesson_ids
        assert stale_rule_id not in rule_ids
    finally:
        # The job_id was inserted directly (no factory_job_factory) so
        # clean it up explicitly. The project_factory teardown won't
        # touch factory_jobs because postulate-conftest doesn't track
        # them.
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM devbrain.factory_jobs WHERE id = %s",
                (job_id,),
            )
        conn.commit()
