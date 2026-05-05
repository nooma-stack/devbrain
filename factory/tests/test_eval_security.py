"""Tests for the eval_security finding parser.

Coverage gate: factory/curator/eval/eval_security.py >= 85%.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from curator.eval.eval_security import parse


def test_parse_empty_findings_list():
    """Clean response yields an empty list — common steady-state path."""
    assert parse({"findings": []}) == []


def test_parse_missing_findings_key():
    """If claude omits the key entirely, treat as empty rather than KeyError.
    Robust because the prompt instructs it but real LLM output drifts."""
    assert parse({}) == []


def test_parse_single_finding_all_fields_populated():
    """Happy path — every optional field present, every UUID parses."""
    rule_id = uuid4()
    memory_id = uuid4()
    response = {
        "findings": [
            {
                "rule_id": str(rule_id),
                "severity": "critical",
                "file": "factory/auth.py",
                "line": 42,
                "message": "SQL string interpolation in build_query()",
                "fix_hint": "Use parameterized query",
                "relevant_memory_id": str(memory_id),
            }
        ]
    }
    findings = parse(response)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == rule_id
    assert f.severity == "critical"
    assert f.file == "factory/auth.py"
    assert f.line == 42
    assert f.message == "SQL string interpolation in build_query()"
    assert f.fix_hint == "Use parameterized query"
    assert f.relevant_memory_id == memory_id


def test_parse_finding_with_null_rule_id_is_heuristic():
    """rule_id=None marks a finding the agent caught heuristically (not
    anchored to a memory row)."""
    response = {
        "findings": [
            {
                "rule_id": None,
                "severity": "important",
                "file": "factory/x.py",
                "line": 1,
                "message": "Heuristic catch — no backing rule.",
                "fix_hint": "Promote to a tracked rule once stable.",
                "relevant_memory_id": str(uuid4()),
            }
        ]
    }
    findings = parse(response)
    assert findings[0].rule_id is None
    assert findings[0].relevant_memory_id is not None


def test_parse_finding_with_null_relevant_memory_id_is_brief_miss():
    """relevant_memory_id=None means the finding wasn't surfaced by the
    curator's brief — that's the signal that drives Phase 6d refinement."""
    response = {
        "findings": [
            {
                "rule_id": str(uuid4()),
                "severity": "critical",
                "file": "factory/y.py",
                "line": 10,
                "message": "Logged secret detected.",
                "fix_hint": "Redact before logging.",
                "relevant_memory_id": None,
            }
        ]
    }
    findings = parse(response)
    assert findings[0].rule_id is not None
    assert findings[0].relevant_memory_id is None


def test_parse_finding_with_both_uuids_null():
    """Heuristic catch + brief miss — the most common shape for early
    Phase 6 runs before the curator is well-trained."""
    response = {
        "findings": [
            {
                "rule_id": None,
                "severity": "minor",
                "file": "factory/z.py",
                "line": 5,
                "message": "Defense-in-depth gap.",
                "fix_hint": "Add input length cap.",
                "relevant_memory_id": None,
            }
        ]
    }
    findings = parse(response)
    assert findings[0].rule_id is None
    assert findings[0].relevant_memory_id is None


def test_parse_multiple_findings_of_different_severities():
    """Each finding carries its own severity — order is preserved."""
    response = {
        "findings": [
            {
                "rule_id": None, "severity": "critical",
                "file": "a.py", "line": 1, "message": "m1",
                "fix_hint": "f1", "relevant_memory_id": None,
            },
            {
                "rule_id": None, "severity": "important",
                "file": "b.py", "line": 2, "message": "m2",
                "fix_hint": "f2", "relevant_memory_id": None,
            },
            {
                "rule_id": None, "severity": "minor",
                "file": "c.py", "line": 3, "message": "m3",
                "fix_hint": "f3", "relevant_memory_id": None,
            },
        ]
    }
    findings = parse(response)
    assert [f.severity for f in findings] == ["critical", "important", "minor"]
    assert [f.file for f in findings] == ["a.py", "b.py", "c.py"]


def test_parse_missing_optional_line_defaults_to_none():
    """Findings that don't pin a line still parse — `line` is optional."""
    response = {
        "findings": [
            {
                "rule_id": None,
                "severity": "minor",
                "file": "factory/dep.py",
                "message": "Dependency CVE notice without a specific line.",
                "fix_hint": "Bump to a patched version.",
                "relevant_memory_id": None,
            }
        ]
    }
    findings = parse(response)
    assert findings[0].line is None
    assert findings[0].file == "factory/dep.py"


def test_parse_missing_optional_fix_hint_defaults_to_empty():
    """fix_hint is optional in real responses (prompts request it but
    LLMs occasionally omit it)."""
    response = {
        "findings": [
            {
                "rule_id": None,
                "severity": "minor",
                "file": "factory/z.py",
                "line": 1,
                "message": "Missing rate limit on public endpoint.",
                "relevant_memory_id": None,
            }
        ]
    }
    findings = parse(response)
    assert findings[0].fix_hint == ""


def test_parse_invalid_severity_raises():
    """Unknown severity must fail loudly — that's a prompt regression
    we want surfaced, not silently dropped."""
    response = {
        "findings": [
            {
                "rule_id": None,
                "severity": "catastrophic",  # not in the Literal
                "file": "x.py",
                "line": 1,
                "message": "m",
                "fix_hint": "f",
                "relevant_memory_id": None,
            }
        ]
    }
    with pytest.raises(Exception):  # Pydantic ValidationError
        parse(response)


def test_parse_invalid_uuid_raises():
    """A malformed rule_id is a prompt regression — fail loudly."""
    response = {
        "findings": [
            {
                "rule_id": "not-a-uuid",
                "severity": "minor",
                "file": "x.py",
                "line": 1,
                "message": "m",
                "fix_hint": "f",
                "relevant_memory_id": None,
            }
        ]
    }
    with pytest.raises(ValueError):
        parse(response)


def test_parse_uuid_strings_become_uuid_objects():
    """Sanity check: rule_id and relevant_memory_id are UUID instances
    on the parsed model, not strings."""
    response = {
        "findings": [
            {
                "rule_id": str(uuid4()),
                "severity": "critical",
                "file": "x.py",
                "line": 1,
                "message": "m",
                "fix_hint": "f",
                "relevant_memory_id": str(uuid4()),
            }
        ]
    }
    f = parse(response)[0]
    assert isinstance(f.rule_id, UUID)
    assert isinstance(f.relevant_memory_id, UUID)
