"""End-to-end integration test for Step 6 eval phase.

Exercises the full IMPLEMENTING -> REVIEWING pathway:
  1. State machine fires the eval phase hook
  2. _load_brief / _load_plan / _load_diff read from substrate storage
  3. run_evals invokes the eval agents (mocked subprocess.run returns
     clean — zero findings)
  4. apply_feedback_signals walks the brief, fires signal #3 for every
     in-brief memory (no findings -> no signal #1, no signal #2)
  5. Each lesson at current_streak=2 hits the GRADUATION_STREAK_THRESHOLD
     of 3 and graduates to tier='rule', graduated_at is stamped
  6. refine_applies_when no-ops (empty refinement_queue)
  7. demote_low_precision_rules no-ops (the just-graduated rules have
     hit_count=0 + effective_hit_count=1 = precision 1.0)

This is the verification gate for Phase 6e Task 6e-3 — confirms the
state machine wiring works end-to-end across all four curator modules
(eval/runner, graduation, refinement, plus the brief substrate).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from state_machine import FactoryDB, JobStatus


@pytest.fixture
def db(database_url):
    return FactoryDB(database_url)


def _clean_eval_response() -> str:
    """JSON payload returned by the mocked claude subprocess — zero findings."""
    return json.dumps({"findings": []})


def _stash_brief_and_advance_to_implementing(
    conn, job_id, lesson_ids
):
    """Write a brief snapshot pointing at the given lesson_ids into
    factory_jobs.curator_brief, then UPDATE the job into IMPLEMENTING
    state directly so the state machine has a legal IMPLEMENTING ->
    REVIEWING transition to fire.

    Walking the QUEUED -> PLANNING -> IMPLEMENTING path naturally would
    invoke generate_brief at the QUEUED -> PLANNING step (the existing
    Step 5d hook), which would overwrite our hand-crafted brief — so we
    UPDATE the row directly here.
    """
    brief = {
        "version": "1.0",
        "job_id": str(job_id),
        "rules": [],
        "lessons": [
            {
                "id": str(mid),
                "kind": "pattern",
                "title": "test-lesson",
                "content_excerpt": "...",
                "tier": "lesson",
                "strength": "1.0",
                "last_cascade_at": None,
            }
            for mid in lesson_ids
        ],
        "relevant_decisions": [],
        "recent_cascade_signals": [],
        "generated_at": "2026-05-04T00:00:00+00:00",
    }
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.factory_jobs "
            "SET curator_brief = %s::jsonb, status = 'implementing', "
            "    current_phase = 'implementing' "
            "WHERE id = %s",
            (json.dumps(brief), job_id),
        )
        # Also write a placeholder plan_doc artifact so _load_plan
        # returns non-empty (the eval prompts handle either, but this
        # mirrors the real pipeline shape).
        cur.execute(
            "INSERT INTO devbrain.factory_artifacts "
            "(job_id, phase, artifact_type, content) "
            "VALUES (%s, 'planning', 'plan_doc', 'placeholder plan')",
            (job_id,),
        )
    conn.commit()


@pytest.mark.db
def test_implementing_to_reviewing_runs_full_eval_pipeline(
    db, conn, project_factory, memory_factory, factory_job_factory
):
    """Full end-to-end: 3 lessons at streak=2 graduate after one clean tick."""
    project = project_factory("e2e_step6")

    # Three lessons, each pre-seeded at current_streak=2 so a single
    # signal #3 (success) crosses the GRADUATION_STREAK_THRESHOLD of 3.
    lessons = [
        memory_factory(project["id"], tier="lesson", content=f"lesson_{i}")
        for i in range(3)
    ]
    with conn.cursor() as cur:
        for lesson in lessons:
            cur.execute(
                "UPDATE devbrain.memory "
                "SET current_streak = 2 "
                "WHERE id = %s",
                (lesson["id"],),
            )
    conn.commit()

    # Need a FactoryJob row owned by this project. The factory_job_factory
    # commits a queued row by default; we patch it into IMPLEMENTING below.
    # job_id flows back as a UUID object from psycopg2's UUID adapter;
    # state_machine.transition expects a str (it slices job_id[:8] for
    # log lines), so coerce here.
    job_row = factory_job_factory(project_id=project["id"], spec="e2e step6")
    job_id = str(job_row["id"])

    _stash_brief_and_advance_to_implementing(
        conn, job_id, [lesson["id"] for lesson in lessons]
    )

    # Mock subprocess.run so the eval agents return zero-finding payloads
    # without invoking real claude. Returns a CompletedProcess-like mock
    # for both agent calls.
    clean_proc = MagicMock(
        returncode=0,
        stdout=_clean_eval_response(),
        stderr="",
    )

    try:
        with patch(
            "curator.eval.runner.subprocess.run",
            return_value=clean_proc,
        ) as mock_run:
            # Trigger the IMPLEMENTING -> REVIEWING transition. This fires
            # _run_eval_phase via the state machine hook.
            result = db.transition(job_id, JobStatus.REVIEWING)

        assert result.status == JobStatus.REVIEWING
        # All three LLM eval agents (security + test + perf — Step 8) should
        # have been invoked. eval_lint is subprocess-driven and uses its own
        # subprocess.run patch path, so it doesn't count here.
        assert mock_run.call_count == 3

        # Assert all 3 lessons graduated to rules with graduated_at set.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, tier, graduated_at, current_streak "
                "FROM devbrain.memory "
                "WHERE id = ANY(%s)",
                ([lesson["id"] for lesson in lessons],),
            )
            rows = cur.fetchall()

        assert len(rows) == 3
        for _id, tier, graduated_at, streak in rows:
            assert tier == "rule", f"memory {_id} did not graduate"
            assert graduated_at is not None
            assert streak == 3
    finally:
        # The eval runner persists per-finding (or per-summary) artifact
        # rows that FK to factory_jobs.id. The factory_job_factory teardown
        # would otherwise hit a foreign-key violation deleting the job —
        # purge artifacts first. project_factory teardown also deletes
        # factory_artifacts via cascade memory cleanup but factory_jobs
        # cleanup runs in factory_job_factory which doesn't know about it.
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM devbrain.factory_artifacts WHERE job_id = %s",
                (job_id,),
            )
        conn.commit()
