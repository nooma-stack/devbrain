"""Tests for the eval_test finding parser.

Schema is identical to eval_security in v3.0 — these tests mirror
test_eval_security with eval_test-flavored fixtures so a future
divergence (if eval_test gets a richer shape) is easy to spot in diffs.

Coverage gate: factory/curator/eval/eval_test.py >= 85%.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from curator.eval.eval_test import parse


def test_parse_empty_findings_list():
    """Clean diff with adequate tests yields an empty list."""
    assert parse({"findings": []}) == []


def test_parse_missing_findings_key():
    """Defensive: missing key is treated as empty rather than KeyError."""
    assert parse({}) == []


def test_parse_single_finding_all_fields_populated():
    """Happy path — coverage gap finding with all metadata."""
    rule_id = uuid4()
    memory_id = uuid4()
    response = {
        "findings": [
            {
                "rule_id": str(rule_id),
                "severity": "critical",
                "file": "factory/widget.py",
                "line": 17,
                "message": "widget.bake() has no test coverage.",
                "fix_hint": "Add a unit test that exercises bake() success and failure paths.",
                "relevant_memory_id": str(memory_id),
            }
        ]
    }
    findings = parse(response)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == rule_id
    assert f.severity == "critical"
    assert f.file == "factory/widget.py"
    assert f.line == 17
    assert "no test coverage" in f.message
    assert f.relevant_memory_id == memory_id


def test_parse_finding_with_null_rule_id_is_heuristic():
    """A test-quality issue spotted by heuristic (not anchored to a rule)."""
    response = {
        "findings": [
            {
                "rule_id": None,
                "severity": "minor",
                "file": "factory/tests/test_x.py",
                "line": 22,
                "message": "Snapshot test asserts on a timestamp — brittle.",
                "fix_hint": "Strip volatile fields before comparing.",
                "relevant_memory_id": str(uuid4()),
            }
        ]
    }
    findings = parse(response)
    assert findings[0].rule_id is None
    assert findings[0].relevant_memory_id is not None


def test_parse_finding_with_null_relevant_memory_id_is_brief_miss():
    """Coverage gap that wasn't surfaced by the curator brief — drives
    Phase 6d refinement."""
    response = {
        "findings": [
            {
                "rule_id": str(uuid4()),
                "severity": "important",
                "file": "factory/y.py",
                "line": 5,
                "message": "Edge case from spec is untested.",
                "fix_hint": "Add a parametrize case for the empty-input branch.",
                "relevant_memory_id": None,
            }
        ]
    }
    findings = parse(response)
    assert findings[0].rule_id is not None
    assert findings[0].relevant_memory_id is None


def test_parse_finding_with_both_uuids_null():
    """Heuristic test-quality catch + brief miss."""
    response = {
        "findings": [
            {
                "rule_id": None,
                "severity": "minor",
                "file": "factory/tests/test_z.py",
                "line": 1,
                "message": "Asserts on mock.called_once_with instead of return value.",
                "fix_hint": "Assert on the function's observable output.",
                "relevant_memory_id": None,
            }
        ]
    }
    findings = parse(response)
    assert findings[0].rule_id is None
    assert findings[0].relevant_memory_id is None


def test_parse_multiple_findings_of_different_severities():
    """Order preserved across severities — coverage > brittleness > smell."""
    response = {
        "findings": [
            {
                "rule_id": None, "severity": "critical",
                "file": "a.py", "line": 1, "message": "no test",
                "fix_hint": "f1", "relevant_memory_id": None,
            },
            {
                "rule_id": None, "severity": "important",
                "file": "b.py", "line": 2, "message": "missing edge case",
                "fix_hint": "f2", "relevant_memory_id": None,
            },
            {
                "rule_id": None, "severity": "minor",
                "file": "c.py", "line": 3, "message": "test smell",
                "fix_hint": "f3", "relevant_memory_id": None,
            },
        ]
    }
    findings = parse(response)
    assert [f.severity for f in findings] == ["critical", "important", "minor"]


def test_parse_missing_optional_line_defaults_to_none():
    """Missing-test findings often have no line to pin to (the gap IS
    the absence of a line)."""
    response = {
        "findings": [
            {
                "rule_id": None,
                "severity": "important",
                "file": "factory/feature.py",
                "message": "New feature has no test file.",
                "fix_hint": "Add factory/tests/test_feature.py.",
                "relevant_memory_id": None,
            }
        ]
    }
    findings = parse(response)
    assert findings[0].line is None


def test_parse_missing_optional_fix_hint_defaults_to_empty():
    """fix_hint is optional even though prompts request it."""
    response = {
        "findings": [
            {
                "rule_id": None,
                "severity": "minor",
                "file": "factory/z.py",
                "line": 1,
                "message": "Test asserts on internal log output.",
                "relevant_memory_id": None,
            }
        ]
    }
    findings = parse(response)
    assert findings[0].fix_hint == ""


def test_parse_invalid_severity_raises():
    """Unknown severity must fail loudly."""
    response = {
        "findings": [
            {
                "rule_id": None,
                "severity": "wow_super_bad",
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
    """Malformed UUID is a prompt regression."""
    response = {
        "findings": [
            {
                "rule_id": None,
                "severity": "minor",
                "file": "x.py",
                "line": 1,
                "message": "m",
                "fix_hint": "f",
                "relevant_memory_id": "obviously-not-a-uuid",
            }
        ]
    }
    with pytest.raises(ValueError):
        parse(response)


def test_parse_uuid_strings_become_uuid_objects():
    """Sanity: parsed UUIDs are UUID instances, not strings."""
    response = {
        "findings": [
            {
                "rule_id": str(uuid4()),
                "severity": "important",
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
