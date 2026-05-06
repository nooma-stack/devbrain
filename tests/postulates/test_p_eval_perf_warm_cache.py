"""P_eval_perf_warm_cache — eval_perf invocation cost validates cache hit.

POSTULATE
---------
When eval_perf runs third in the LLM eval chain (after eval_security and
eval_test), the user-message payload sent to the claude subprocess is
IDENTICAL to the payloads sent to the earlier agents. This is the
structural contract for prompt-cache reuse: same bytes in → cache hit →
~10% cost.

The postulate does NOT invoke a real claude subprocess (that would
require API credits and be slow/unreliable in CI). Instead it asserts
the structural condition for cache-hit eligibility: all three agents
receive the same `cached_context` string as their stdin input.

STATUS
------
Activated in Atlas Step 8 — eval_perf joins the cached eval chain.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

# The postulate suite runs from repo root; the curator module lives under
# factory/. Add factory/ to sys.path so the import resolves regardless of
# where pytest is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "factory"))

from curator.eval.runner import run_evals  # noqa: E402


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


def _proc_result(findings=None):
    payload = {"findings": findings or []}
    return MagicMock(
        returncode=0,
        stdout=json.dumps(payload),
        stderr="",
    )


def test_eval_perf_receives_same_cached_context_as_security_and_test():
    """The stdin payload sent to eval_perf is byte-identical to the
    payloads sent to eval_security and eval_test. This is the structural
    pre-condition for prompt-cache hits on the third agent call.

    Cache reuse requires:
    1. Same user-message body (stdin) across all three agents.
    2. Different system prompts (only the system prompt varies).

    This postulate validates condition 1.
    """
    job_id = uuid4()
    conn = _FakeConn()
    captured_inputs: list[str] = []
    captured_system_prompts: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_inputs.append(kwargs.get("input", ""))
        # Extract --system-prompt from cmd list.
        idx = cmd.index("--system-prompt")
        captured_system_prompts.append(cmd[idx + 1])
        return _proc_result()

    with patch("curator.eval.runner.subprocess.run", side_effect=fake_run):
        results = run_evals(
            conn, job_id,
            brief={"version": "1.0", "rules": []},
            plan="implement the feature",
            diff="+ added a new query inside a for loop\n",
        )

    # Three agents must have run.
    assert len(results) == 3
    agent_names = [r.agent_name for r in results]
    assert agent_names == ["eval_security", "eval_test", "eval_perf"]

    # CORE POSTULATE: all three stdin payloads are identical.
    # This is the cache-hit pre-condition.
    assert len(captured_inputs) == 3
    assert captured_inputs[0] == captured_inputs[1] == captured_inputs[2], (
        "eval_perf received a different stdin payload than eval_security/"
        "eval_test — this breaks prompt-cache reuse and doubles cost."
    )

    # Sanity: each agent used a different system prompt (the one varying axis).
    assert captured_system_prompts[0] != captured_system_prompts[1]
    assert captured_system_prompts[0] != captured_system_prompts[2]
    assert captured_system_prompts[1] != captured_system_prompts[2]

    # Sanity: eval_perf prompt file is perf-specific.
    assert "eval_perf" in captured_system_prompts[2]
