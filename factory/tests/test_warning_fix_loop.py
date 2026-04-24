"""Tests for the WARNING-triggered fix-loop gate.

Covers the 2026-04-23 change where reviewer WARNING findings can also
route a job through FIX_LOOP when the new
`factory.fix_loop.warnings_trigger_retry` tier is on (default True).

The gate itself lives at the end of `_run_review` in orchestrator.py.
We exercise it end-to-end by stubbing `run_cli` (no actual claude call)
and `subprocess.run` (no `git diff main...HEAD`) and reading back the
post-review job status.
"""
import json as _json

import pytest

import orchestrator as orch_mod
from orchestrator import FactoryOrchestrator
from state_machine import FactoryDB, JobStatus
from config import DATABASE_URL

TEST_TITLE_PREFIX = "warning_fix_loop_test_"


def _with_json(prose: str, findings: list[dict]) -> str:
    """Append a JSON findings block to a prose review body. Used to
    exercise the JSON-path of the post-PR-21b1b68a parser; the prose
    side stays in place so fallback regex counts would still match
    the same numbers (parity check for the parser swap).
    """
    return (
        f"{prose}\n\n```json findings\n"
        f"{_json.dumps({'findings': findings})}\n```\n"
    )


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture(autouse=True)
def cleanup(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.factory_jobs WHERE title LIKE %s",
            (f"{TEST_TITLE_PREFIX}%",),
        )
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            cur.execute(
                "DELETE FROM devbrain.factory_cleanup_reports "
                "WHERE job_id = ANY(%s)",
                (ids,),
            )
            cur.execute(
                "DELETE FROM devbrain.factory_artifacts "
                "WHERE job_id = ANY(%s)",
                (ids,),
            )
            cur.execute(
                "UPDATE devbrain.factory_jobs SET blocked_by_job_id = NULL "
                "WHERE blocked_by_job_id = ANY(%s)",
                (ids,),
            )
            cur.execute(
                "DELETE FROM devbrain.factory_jobs WHERE id = ANY(%s)",
                (ids,),
            )
        conn.commit()


@pytest.fixture
def orch():
    return FactoryOrchestrator(DATABASE_URL)


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeCLIResult:
    """Shape-compatible with cli_executor.CLIResult for what _run_review reads."""

    def __init__(self, stdout: str):
        self.cli = "claude"
        self.exit_code = 0
        self.stdout = stdout
        self.stderr = ""
        self.success = True


def _make_implementing_job(db: FactoryDB, title: str):
    """Create a job and walk it QUEUED → PLANNING → IMPLEMENTING so the
    gate under test sees the transition-to-REVIEWING _run_review performs."""
    job_id = db.create_job(project_slug="devbrain", title=title, spec="test")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.IMPLEMENTING)
    return db.get_job(job_id)


def _stub_review_env(monkeypatch, *, arch_stdout: str, sec_stdout: str):
    """Stub run_cli (both arch + security reviews) and subprocess.run
    (git diff) so _run_review exercises the gate without touching CLIs
    or git. run_cli returns arch_stdout then sec_stdout in order."""
    responses = iter([_FakeCLIResult(arch_stdout), _FakeCLIResult(sec_stdout)])

    def fake_run_cli(*args, **kwargs):
        return next(responses)

    monkeypatch.setattr(orch_mod, "run_cli", fake_run_cli)
    monkeypatch.setattr(
        orch_mod.subprocess, "run",
        lambda *a, **k: _FakeCompleted(stdout="diff --git a/x b/x\n"),
    )


def test_blocking_triggers_fix_loop_regardless_of_config(orch, db, monkeypatch):
    """BLOCKING findings always route to FIX_LOOP, even when the WARNING
    trigger tier is disabled — BLOCKING is not gated by the flag."""
    monkeypatch.setattr(
        orch_mod, "FACTORY_FIX_LOOP_WARNINGS_TRIGGER_RETRY", False
    )
    job = _make_implementing_job(
        db, f"{TEST_TITLE_PREFIX}blocking_always_fixes"
    )

    _stub_review_env(
        monkeypatch,
        arch_stdout=_with_json(
            "1. BLOCKING: missing null check at x.py:42",
            [{"severity": "BLOCKING", "title": "null-check",
              "body": "missing null check at x.py:42"}],
        ),
        sec_stdout=_with_json("(no findings)", []),
    )

    result = orch._run_review(job)

    assert result.status == JobStatus.FIX_LOOP
    assert result.metadata.get("blocking_findings") == 1
    assert result.metadata.get("trigger_reason") == "blocking"


def test_warning_triggers_fix_loop_when_flag_true(orch, db, monkeypatch):
    """WARNING-only findings route to FIX_LOOP when the flag is on."""
    monkeypatch.setattr(
        orch_mod, "FACTORY_FIX_LOOP_WARNINGS_TRIGGER_RETRY", True
    )
    job = _make_implementing_job(
        db, f"{TEST_TITLE_PREFIX}warning_flag_on"
    )

    _stub_review_env(
        monkeypatch,
        arch_stdout=_with_json(
            "1. WARNING: suboptimal pattern at a.py:10\n"
            "2. WARNING: missing docstring at b.py:5\n",
            [
                {"severity": "WARNING", "title": "suboptimal-pattern",
                 "body": "suboptimal pattern at a.py:10"},
                {"severity": "WARNING", "title": "missing-docstring",
                 "body": "missing docstring at b.py:5"},
            ],
        ),
        sec_stdout=_with_json("(no findings)", []),
    )

    result = orch._run_review(job)

    assert result.status == JobStatus.FIX_LOOP
    assert result.metadata.get("blocking_findings") == 0
    assert result.metadata.get("warning_findings") == 2
    assert result.metadata.get("trigger_reason") == "warning"


def test_warning_skipped_when_flag_false(orch, db, monkeypatch):
    """WARNING-only findings fall through to QA when the flag is off —
    preserves the pre-2026-04-23 BLOCKING-only gate behavior."""
    monkeypatch.setattr(
        orch_mod, "FACTORY_FIX_LOOP_WARNINGS_TRIGGER_RETRY", False
    )
    job = _make_implementing_job(
        db, f"{TEST_TITLE_PREFIX}warning_flag_off"
    )

    _stub_review_env(
        monkeypatch,
        arch_stdout=_with_json(
            "1. WARNING: suboptimal pattern at a.py:10\n"
            "2. WARNING: missing docstring at b.py:5\n",
            [
                {"severity": "WARNING", "title": "suboptimal-pattern",
                 "body": "suboptimal pattern at a.py:10"},
                {"severity": "WARNING", "title": "missing-docstring",
                 "body": "missing docstring at b.py:5"},
            ],
        ),
        sec_stdout=_with_json("(no findings)", []),
    )

    result = orch._run_review(job)

    assert result.status == JobStatus.QA


def test_extract_warning_items_from_review_content(orch, db):
    """Regex-fallback canary — this case intentionally has NO JSON
    block so the helpers exercise the PR #32 fallback path. Don't
    add a JSON block here; that would mask a regression in the
    fallback contract reviewers land on when they don't comply
    with the JSON format.

    Matches the same extraction path _run_fix uses inline when
    collecting prior warning findings for the fix prompt (see
    orchestrator.py _run_fix around the `latest_warning` loop).
    """
    from orchestrator import _extract_warning_items

    text = (
        "1. WARNING: first warning at a.py:1\n"
        "2. WARNING: second warning at b.py:2\n"
        "3. BLOCKING: some blocking issue at c.py:3\n"
    )
    items = _extract_warning_items(text)
    assert len(items) == 2
    assert "first warning" in items[0]
    assert "second warning" in items[1]

    sec_text = "1. WARNING: security warning at c.py:3"
    sec_items = _extract_warning_items(sec_text)
    assert len(sec_items) == 1
    assert "security warning" in sec_items[0]
