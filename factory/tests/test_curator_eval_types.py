"""Tests for the eval finding/result Pydantic models.

Covers JSON round-trip + validation rules. The contract is stable as soon
as Phase 6b (eval runner) starts producing these — bumping requires a new
version literal.
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from curator.eval.types import EvalFinding, EvalResult


def _finding(**overrides) -> EvalFinding:
    """Build a valid finding with sensible defaults; overrides win."""
    payload = {
        "rule_id": uuid4(),
        "severity": "critical",
        "file": "src/auth.py",
        "line": 42,
        "message": "SQL string interpolation",
        "fix_hint": "Use parameterized query",
        "relevant_memory_id": uuid4(),
    }
    payload.update(overrides)
    return EvalFinding(**payload)


def _result(**overrides) -> EvalResult:
    payload = {
        "version": "1.0",
        "job_id": uuid4(),
        "agent_name": "eval_security",
        "findings": [_finding()],
        "elapsed_ms": 1234,
        "started_at": datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc),
        "error": None,
    }
    payload.update(overrides)
    return EvalResult(**payload)


def test_eval_finding_roundtrip():
    """A finding should survive a full JSON round-trip with no data loss."""
    original = _finding()
    rehydrated = EvalFinding.model_validate_json(original.model_dump_json())
    assert rehydrated == original


def test_eval_result_roundtrip():
    """An EvalResult — including its findings list — should round-trip clean."""
    original = _result()
    rehydrated = EvalResult.model_validate_json(original.model_dump_json())
    assert rehydrated == original


def test_eval_finding_rejects_invalid_severity():
    """severity must be one of critical|important|minor."""
    with pytest.raises(ValidationError):
        _finding(severity="catastrophic")


def test_eval_result_rejects_invalid_agent_name():
    """agent_name is locked to the two known eval agents."""
    with pytest.raises(ValidationError):
        _result(agent_name="eval_performance")


def test_eval_result_rejects_unknown_version():
    """Forward-incompat versions must not silently parse — bumping the
    contract requires a new Literal value."""
    with pytest.raises(ValidationError):
        _result(version="2.0")


def test_eval_finding_allows_null_rule_id():
    """Heuristic findings (no backing memory row) carry rule_id=None;
    relevant_memory_id can also be None for findings the brief missed."""
    finding = _finding(rule_id=None, relevant_memory_id=None)
    assert finding.rule_id is None
    assert finding.relevant_memory_id is None
    # Round-trip preserves the nulls.
    rehydrated = EvalFinding.model_validate_json(finding.model_dump_json())
    assert rehydrated == finding
