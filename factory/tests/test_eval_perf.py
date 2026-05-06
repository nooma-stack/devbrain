"""Tests for the eval_perf finding parser.

eval_perf detects performance antipatterns (N+1 queries, unbounded
SELECTs, missing indexes, sync I/O in async contexts, O(N²) operations).
The parser mirrors eval_security / eval_test — same JSON schema, same
permissive fallbacks for optional fields.

Coverage gate: factory/curator/eval/eval_perf.py >= 85%.
"""
from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from curator.eval.eval_perf import parse


def test_parse_empty_findings_list():
    """Clean diff with no perf issues yields an empty list."""
    assert parse({"findings": []}) == []


def test_parse_missing_findings_key():
    """Defensive: missing key is treated as empty rather than KeyError."""
    assert parse({}) == []


def test_parse_single_finding_n_plus_one():
    """Happy path — N+1 finding with all fields populated."""
    rule_id = uuid4()
    memory_id = uuid4()
    response = {
        "findings": [
            {
                "rule_id": str(rule_id),
                "severity": "critical",
                "file": "factory/views.py",
                "line": 88,
                "message": "N+1 query: SELECT inside loop over user IDs.",
                "fix_hint": "Use WHERE id IN (...) to batch-fetch in one query.",
                "relevant_memory_id": str(memory_id),
            }
        ]
    }
    findings = parse(response)
    assert len(findings) == 1
    f = findings[0]
    assert f.rule_id == rule_id
    assert f.severity == "critical"
    assert f.file == "factory/views.py"
    assert f.line == 88
    assert "N+1" in f.message
    assert f.relevant_memory_id == memory_id


def test_parse_finding_with_null_rule_id_is_heuristic():
    """A perf issue caught heuristically (not anchored to a memory rule)."""
    response = {
        "findings": [
            {
                "rule_id": None,
                "severity": "important",
                "file": "factory/reports.py",
                "line": 34,
                "message": "Unbounded SELECT on reports table — missing LIMIT.",
                "fix_hint": "Add LIMIT clause or paginator.",
                "relevant_memory_id": str(uuid4()),
            }
        ]
    }
    findings = parse(response)
    assert findings[0].rule_id is None
    assert findings[0].relevant_memory_id is not None


def test_parse_finding_with_null_relevant_memory_id_is_brief_miss():
    """Perf finding not surfaced by curator brief — drives refinement."""
    response = {
        "findings": [
            {
                "rule_id": str(uuid4()),
                "severity": "important",
                "file": "factory/api.py",
                "line": 12,
                "message": "Sync requests.get() called inside async handler.",
                "fix_hint": "Use aiohttp or httpx async client.",
                "relevant_memory_id": None,
            }
        ]
    }
    findings = parse(response)
    assert findings[0].rule_id is not None
    assert findings[0].relevant_memory_id is None


def test_parse_finding_with_both_uuids_null():
    """Heuristic perf catch + brief miss — common before curator is trained."""
    response = {
        "findings": [
            {
                "rule_id": None,
                "severity": "minor",
                "file": "factory/utils.py",
                "line": 5,
                "message": "O(N²) loop on user-supplied list.",
                "fix_hint": "Use a set for O(1) membership tests.",
                "relevant_memory_id": None,
            }
        ]
    }
    findings = parse(response)
    assert findings[0].rule_id is None
    assert findings[0].relevant_memory_id is None


def test_parse_multiple_findings_of_different_severities():
    """Order is preserved; each finding carries its own severity."""
    response = {
        "findings": [
            {
                "rule_id": None, "severity": "critical",
                "file": "a.py", "line": 1, "message": "N+1 in request handler",
                "fix_hint": "batch", "relevant_memory_id": None,
            },
            {
                "rule_id": None, "severity": "important",
                "file": "b.py", "line": 2, "message": "missing index on JOIN",
                "fix_hint": "add index", "relevant_memory_id": None,
            },
            {
                "rule_id": None, "severity": "minor",
                "file": "c.py", "line": 3, "message": "O(N²) on small input",
                "fix_hint": "use set", "relevant_memory_id": None,
            },
        ]
    }
    findings = parse(response)
    assert [f.severity for f in findings] == ["critical", "important", "minor"]
    assert [f.file for f in findings] == ["a.py", "b.py", "c.py"]


def test_parse_missing_optional_line_defaults_to_none():
    """Missing-index findings often have no specific line to pin to."""
    response = {
        "findings": [
            {
                "rule_id": None,
                "severity": "important",
                "file": "factory/models.py",
                "message": "JOIN on non-indexed column `user_id` in HotTable.",
                "fix_hint": "CREATE INDEX idx_hottable_user_id ON hottable(user_id);",
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
                "message": "Sync I/O in async context.",
                "relevant_memory_id": None,
            }
        ]
    }
    findings = parse(response)
    assert findings[0].fix_hint == ""


def test_parse_invalid_severity_raises():
    """Unknown severity must fail loudly — that's a prompt regression."""
    response = {
        "findings": [
            {
                "rule_id": None,
                "severity": "blazing_fast",  # not in the Literal
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
                "rule_id": "not-a-valid-uuid",
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
    """Parsed UUIDs are UUID instances, not strings."""
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
