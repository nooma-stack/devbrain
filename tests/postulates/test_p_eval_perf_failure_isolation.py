"""P_eval_perf_failure_isolation — eval_lint still runs when eval_perf errors.

POSTULATE
---------
If eval_perf (third LLM agent) raises an exception, the static eval
chain (eval_lint) still runs to completion via run_all_evals(). Failure
isolation is per-agent — one agent failure must not cascade to the other
chain.

This postulate also implicitly validates that the two chains (LLM vs
static) are independent: a crash in the LLM chain does not abort the
static chain.

STATUS
------
Activated in Atlas Step 8/9 — run_all_evals() orchestrator ships with
both chains.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "factory"))

from curator.eval.runner import run_all_evals  # noqa: E402
from curator.eval.types import EvalResult  # noqa: E402


class _FakeCursor:
    def __init__(self):
        self.executed = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self):
        self.cursor_obj = _FakeCursor()
        self.commits = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1


def _ok_result():
    return MagicMock(
        returncode=0,
        stdout=json.dumps({"findings": []}),
        stderr="",
    )


def _lint_result_skipped():
    """Synthetic eval_lint result returned by a mocked eval_lint.run()."""
    return EvalResult(
        version="1.0",
        job_id=uuid4(),
        agent_name="eval_lint",
        findings=[],
        elapsed_ms=1,
        started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        skipped="no diff files",
    )


def test_eval_lint_runs_when_eval_perf_fails():
    """When eval_perf crashes (subprocess returns non-zero), eval_lint
    still runs. The combined result list includes all four agents with
    eval_perf carrying an error and eval_lint running cleanly.
    """
    job_id = uuid4()
    conn = _FakeConn()

    call_idx = {"n": 0}

    def fake_subprocess_run(cmd, **kwargs):
        call_idx["n"] += 1
        if call_idx["n"] == 3:
            # Third call is eval_perf — make it fail.
            return MagicMock(returncode=2, stdout="", stderr="perf agent crash")
        return _ok_result()

    lint_called = {"called": False}

    def fake_lint_run(conn, job_id, diff_files, **kwargs):
        lint_called["called"] = True
        return _lint_result_skipped()

    with (
        patch("curator.eval.runner.subprocess.run", side_effect=fake_subprocess_run),
        patch("curator.eval.eval_lint.run", side_effect=fake_lint_run),
    ):
        results = run_all_evals(
            conn, job_id,
            brief={}, plan="", diff="",
            diff_files=[],
        )

    # Four results: security, test, perf (errored), lint (skipped/clean).
    assert len(results) == 4
    agent_names = [r.agent_name for r in results]
    assert "eval_security" in agent_names
    assert "eval_test" in agent_names
    assert "eval_perf" in agent_names
    assert "eval_lint" in agent_names

    perf_result = next(r for r in results if r.agent_name == "eval_perf")
    assert perf_result.error is not None
    assert "perf agent crash" in perf_result.error or "exit 2" in perf_result.error

    # eval_lint MUST have run despite eval_perf failing.
    assert lint_called["called"], (
        "eval_lint was not called — failure in eval_perf cascaded to the "
        "static eval chain, violating failure-isolation contract."
    )

    lint_result = next(r for r in results if r.agent_name == "eval_lint")
    assert lint_result.error is None or lint_result.skipped is not None
