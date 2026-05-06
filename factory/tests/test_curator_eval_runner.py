"""Tests for the eval runner.

The runner orchestrates two sequential claude-CLI invocations sharing a
warm prompt cache. Tests mock subprocess.run so no real claude is
invoked, and use a fake DB connection so coverage on the persistence
path is observable without a Postgres dependency.

Coverage gate: factory/curator/eval/runner.py >= 85%.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest

from curator.eval.runner import (
    _build_cached_context,
    _persist_findings,
    run_evals,
)
from curator.eval.types import EvalFinding, EvalResult


# ---------------------------------------------------------------- helpers


class _FakeCursor:
    """Minimal cursor that records executed SQL + params for assertions."""

    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.executed.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    """Minimal psycopg2-style connection that captures cursor activity."""

    def __init__(self):
        self.cursor_obj = _FakeCursor()
        self.commits = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1


def _proc_result(stdout: str = '{"findings": []}', returncode: int = 0,
                  stderr: str = "") -> MagicMock:
    """Build a CompletedProcess-like MagicMock matching subprocess.run."""
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


def _security_finding_response() -> dict:
    return {
        "findings": [
            {
                "rule_id": str(uuid4()),
                "severity": "critical",
                "file": "factory/auth.py",
                "line": 42,
                "message": "SQL string interpolation",
                "fix_hint": "Use parameterized query",
                "relevant_memory_id": str(uuid4()),
            }
        ]
    }


def _test_finding_response() -> dict:
    return {
        "findings": [
            {
                "rule_id": None,
                "severity": "important",
                "file": "factory/widget.py",
                "line": 11,
                "message": "no test for widget.bake()",
                "fix_hint": "Add a unit test that covers widget.bake() success path.",
                "relevant_memory_id": None,
            }
        ]
    }


# ----------------------------------------------------- _build_cached_context


def test_build_cached_context_shape_is_stable():
    """Cached context byte-shape is the cache-hit hot path. Any drift
    between security and test invocations breaks the warm cache, so the
    builder must be deterministic across calls with identical inputs."""
    brief = {"version": "1.0", "rules": []}
    plan = "Step 1: do the thing"
    diff = "+ added line\n- removed line"

    a = _build_cached_context(brief, plan, diff)
    b = _build_cached_context(brief, plan, diff)
    assert a == b

    # Sanity: each section is present and labeled.
    assert "## Brief" in a
    assert "## Plan" in a
    assert "## Diff" in a
    # Brief is rendered as indented JSON.
    assert json.dumps(brief, indent=2) in a
    # Plan is verbatim.
    assert plan in a
    # Diff is wrapped in a fenced ```diff block.
    assert "```diff" in a
    assert diff in a


def test_build_cached_context_preserves_brief_structure():
    """The brief json must round-trip — the eval prompts read this back
    out to find `relevant_memory_id` candidates."""
    rule_id = str(uuid4())
    brief = {
        "version": "1.0",
        "rules": [{"id": rule_id, "title": "no_secrets_in_logs"}],
        "lessons": [],
        "relevant_decisions": [],
    }
    ctx = _build_cached_context(brief, "plan", "diff")
    # The id must survive intact for the eval agent to surface it.
    assert rule_id in ctx


# ------------------------------------------------------------- run_evals


def test_run_evals_invokes_all_agents_in_order():
    """eval_security MUST run first (primes cache), eval_test second,
    eval_perf third. Reversing breaks the warm-cache pattern."""
    job_id = uuid4()
    conn = _FakeConn()
    captured_args: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_args.append(list(cmd))
        return _proc_result(stdout=json.dumps({"findings": []}))

    with patch("curator.eval.runner.subprocess.run", side_effect=fake_run):
        results = run_evals(conn, job_id, brief={}, plan="", diff="")

    assert len(results) == 3
    assert results[0].agent_name == "eval_security"
    assert results[1].agent_name == "eval_test"
    assert results[2].agent_name == "eval_perf"
    # All three invocations went through the claude CLI.
    assert all(args[0] == "claude" for args in captured_args)
    assert len(captured_args) == 3


def test_run_evals_passes_same_user_payload_to_all_agents():
    """Cache hit depends on identical user message across calls. The
    runner sends the same `cached_context` as stdin to all subprocess
    calls; only the system-prompt differs."""
    job_id = uuid4()
    conn = _FakeConn()
    captured_inputs: list[str] = []

    def fake_run(cmd, **kwargs):
        captured_inputs.append(kwargs["input"])
        return _proc_result()

    with patch("curator.eval.runner.subprocess.run", side_effect=fake_run):
        run_evals(conn, job_id, brief={"k": "v"}, plan="P", diff="D")

    assert len(captured_inputs) == 3
    # All three agents receive the same cached context body.
    assert captured_inputs[0] == captured_inputs[1] == captured_inputs[2]


def test_run_evals_persists_findings_to_factory_artifacts():
    """Each finding from each agent gets its own factory_artifacts row.
    Empty-result agents emit a single summary row instead."""
    job_id = uuid4()
    conn = _FakeConn()

    # security + test have findings; perf is clean.
    responses = [
        _security_finding_response(),
        _test_finding_response(),
        {"findings": []},
    ]
    response_iter = iter(responses)

    def fake_run(cmd, **kwargs):
        return _proc_result(stdout=json.dumps(next(response_iter)))

    with patch("curator.eval.runner.subprocess.run", side_effect=fake_run):
        results = run_evals(conn, job_id, brief={}, plan="", diff="")

    # Two findings + one summary row (eval_perf clean) -> 3 INSERTs; one commit.
    inserts = [r for r in conn.cursor_obj.executed if r[0].startswith("INSERT")]
    assert len(inserts) == 3
    assert conn.commits == 1
    # security and test each have 1 finding; perf is clean.
    assert len(results[0].findings) == 1
    assert len(results[1].findings) == 1
    assert len(results[2].findings) == 0


def test_run_evals_persists_summary_row_when_no_findings():
    """A clean-diff result still writes one row so the run is observable
    in the dashboard. The artifact_type carries an `_summary` suffix."""
    job_id = uuid4()
    conn = _FakeConn()

    def fake_run(cmd, **kwargs):
        return _proc_result(stdout=json.dumps({"findings": []}))

    with patch("curator.eval.runner.subprocess.run", side_effect=fake_run):
        run_evals(conn, job_id, brief={}, plan="", diff="")

    inserts = [r for r in conn.cursor_obj.executed if r[0].startswith("INSERT")]
    assert len(inserts) == 3  # one summary per agent (security, test, perf)
    # Each insert has artifact_type ending in _summary.
    artifact_types = [params[2] for _, params in inserts]
    assert artifact_types == [
        "eval_security_summary",
        "eval_test_summary",
        "eval_perf_summary",
    ]


def test_run_evals_isolates_agent_failure():
    """When eval_security raises, eval_test and eval_perf still run. The
    failed agent's EvalResult carries findings=[] and a non-null `error`."""
    job_id = uuid4()
    conn = _FakeConn()

    call_idx = {"n": 0}

    def fake_run(cmd, **kwargs):
        call_idx["n"] += 1
        if call_idx["n"] == 1:
            # First (eval_security) call fails.
            return _proc_result(stdout="", returncode=2, stderr="boom")
        return _proc_result(stdout=json.dumps({"findings": []}))

    with patch("curator.eval.runner.subprocess.run", side_effect=fake_run):
        results = run_evals(conn, job_id, brief={}, plan="", diff="")

    assert len(results) == 3
    # eval_security failed.
    assert results[0].agent_name == "eval_security"
    assert results[0].findings == []
    assert results[0].error is not None
    assert "claude exit 2" in results[0].error
    # eval_test still ran cleanly.
    assert results[1].agent_name == "eval_test"
    assert results[1].error is None
    # eval_perf also ran cleanly.
    assert results[2].agent_name == "eval_perf"
    assert results[2].error is None


def test_run_evals_handles_json_decode_error():
    """If claude returns malformed JSON, the agent's EvalResult carries
    the exception in `error`; other agents are unaffected."""
    job_id = uuid4()
    conn = _FakeConn()
    call_idx = {"n": 0}

    def fake_run(cmd, **kwargs):
        call_idx["n"] += 1
        if call_idx["n"] == 1:
            return _proc_result(stdout="<not json>")
        return _proc_result(stdout=json.dumps({"findings": []}))

    with patch("curator.eval.runner.subprocess.run", side_effect=fake_run):
        results = run_evals(conn, job_id, brief={}, plan="", diff="")

    assert results[0].error is not None
    # The error message length is bounded so we don't blow up the
    # factory_artifacts row.
    assert len(results[0].error) <= 500
    # eval_test and eval_perf still ran cleanly.
    assert results[1].error is None
    assert results[2].error is None


def test_run_evals_handles_subprocess_timeout():
    """Subprocess timeouts are caught by the agent isolation block."""
    import subprocess as _sp
    job_id = uuid4()
    conn = _FakeConn()
    call_idx = {"n": 0}

    def fake_run(cmd, **kwargs):
        call_idx["n"] += 1
        if call_idx["n"] == 1:
            raise _sp.TimeoutExpired(cmd=cmd, timeout=120)
        return _proc_result(stdout=json.dumps({"findings": []}))

    with patch("curator.eval.runner.subprocess.run", side_effect=fake_run):
        results = run_evals(conn, job_id, brief={}, plan="", diff="")

    assert results[0].error is not None
    assert results[1].error is None
    assert results[2].error is None


def test_run_evals_passes_system_prompts_per_agent():
    """Each agent must use a distinct --system-prompt body — that's the
    only thing that varies across the three cached calls."""
    job_id = uuid4()
    conn = _FakeConn()
    captured_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(list(cmd))
        return _proc_result()

    with patch("curator.eval.runner.subprocess.run", side_effect=fake_run):
        run_evals(conn, job_id, brief={}, plan="", diff="")

    # Each cmd has --system-prompt followed by the prompt text.
    def _system_prompt(cmd):
        idx = cmd.index("--system-prompt")
        return cmd[idx + 1]

    sp_security = _system_prompt(captured_cmds[0])
    sp_test = _system_prompt(captured_cmds[1])
    sp_perf = _system_prompt(captured_cmds[2])
    # All three prompts are distinct.
    assert sp_security != sp_test
    assert sp_security != sp_perf
    assert sp_test != sp_perf
    # Sanity: the prompt files we shipped land in the right slot.
    assert "eval_security" in sp_security
    assert "eval_test" in sp_test
    assert "eval_perf" in sp_perf


# ----------------------------------------------------- _persist_findings


def test_persist_findings_writes_one_row_per_finding():
    """Each finding becomes one factory_artifacts INSERT."""
    job_id = uuid4()
    conn = _FakeConn()
    findings = [
        EvalFinding(
            rule_id=uuid4(),
            severity="critical",
            file="a.py",
            line=1,
            message="m1",
            fix_hint="f1",
            relevant_memory_id=uuid4(),
        ),
        EvalFinding(
            rule_id=None,
            severity="minor",
            file="b.py",
            line=None,
            message="m2",
            fix_hint="",
            relevant_memory_id=None,
        ),
    ]
    result = EvalResult(
        version="1.0",
        job_id=job_id,
        agent_name="eval_security",
        findings=findings,
        elapsed_ms=42,
        started_at=datetime.now(timezone.utc),
    )
    _persist_findings(conn, job_id, [result])

    inserts = [r for r in conn.cursor_obj.executed if r[0].startswith("INSERT")]
    assert len(inserts) == 2
    # Each row carries phase='reviewing' and artifact_type=agent_name.
    for _, params in inserts:
        assert params[1] == "reviewing"
        assert params[2] == "eval_security"


def test_persist_findings_metadata_includes_error():
    """A failed agent's metadata.error is preserved for the dashboard."""
    job_id = uuid4()
    conn = _FakeConn()
    result = EvalResult(
        version="1.0",
        job_id=job_id,
        agent_name="eval_test",
        findings=[],
        elapsed_ms=0,
        started_at=datetime.now(timezone.utc),
        error="claude exit 2: boom",
    )
    _persist_findings(conn, job_id, [result])

    inserts = [r for r in conn.cursor_obj.executed if r[0].startswith("INSERT")]
    assert len(inserts) == 1
    metadata_json = inserts[0][1][5]
    metadata = json.loads(metadata_json)
    assert metadata["error"] == "claude exit 2: boom"
    assert metadata["agent_name"] == "eval_test"


# ------------------------------------------------------- EvalResult IO


def test_eval_result_round_trips_through_serialization():
    """Sanity check the contract eval_runner produces — downstream
    Phase 6c graduation reads these values back out."""
    job_id = uuid4()
    finding = EvalFinding(
        rule_id=uuid4(),
        severity="important",
        file="x.py",
        line=10,
        message="m",
        fix_hint="f",
        relevant_memory_id=uuid4(),
    )
    original = EvalResult(
        version="1.0",
        job_id=job_id,
        agent_name="eval_security",
        findings=[finding],
        elapsed_ms=42,
        started_at=datetime.now(timezone.utc),
    )
    rehydrated = EvalResult.model_validate_json(original.model_dump_json())
    assert rehydrated == original
    assert rehydrated.findings[0].rule_id == finding.rule_id


# ----------------------------------------------------- _parse_findings


def test_parse_findings_unknown_agent_raises():
    """The internal dispatcher rejects unknown agent names — only the
    registered LLM eval agents are valid."""
    from curator.eval.runner import _parse_findings
    with pytest.raises(ValueError):
        _parse_findings("eval_typo", {"findings": []})


def test_parse_findings_routes_to_correct_module():
    """Each agent name delegates to its own parse module."""
    from curator.eval.runner import _parse_findings

    response = {
        "findings": [
            {
                "rule_id": None,
                "severity": "minor",
                "file": "x.py",
                "line": None,
                "message": "m",
                "fix_hint": "",
                "relevant_memory_id": None,
            }
        ]
    }
    sec = _parse_findings("eval_security", response)
    tst = _parse_findings("eval_test", response)
    perf = _parse_findings("eval_perf", response)
    assert len(sec) == 1
    assert len(tst) == 1
    assert len(perf) == 1
    assert sec[0].file == "x.py"
    assert tst[0].file == "x.py"
    assert perf[0].file == "x.py"
