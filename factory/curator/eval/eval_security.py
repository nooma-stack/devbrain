"""eval_security finding parser.

eval_security checks: auth, injection, secret leakage, dependency CVE.
The prompt instructs claude to emit a JSON list of findings; this parser
maps that to EvalFinding objects.

Schema kept identical to eval_test.parse — both modules consume the
same response shape, only the underlying prompt differs. Bumping the
shape is a coordinated change across both parsers.
"""
from __future__ import annotations

from uuid import UUID

from curator.eval.types import EvalFinding


def parse(response: dict) -> list[EvalFinding]:
    """Map claude's JSON response to an EvalFinding list.

    Expected response shape from the prompt:

    {
      "findings": [
        {
          "rule_id": "uuid-or-null",
          "severity": "critical|important|minor",
          "file": "path/to/file.py",
          "line": 42,
          "message": "...",
          "fix_hint": "...",
          "relevant_memory_id": "uuid-or-null"
        }
      ]
    }

    Missing optional fields fall back to defaults:
    - line: None (finding doesn't pin a line)
    - fix_hint: empty string (parser is permissive — prompts target this
      field but don't always fill it)
    - rule_id, relevant_memory_id: None (heuristic finding / brief miss)
    """
    raw = response.get("findings", [])
    return [_parse_one(item) for item in raw]


def _parse_one(item: dict) -> EvalFinding:
    return EvalFinding(
        rule_id=UUID(item["rule_id"]) if item.get("rule_id") else None,
        severity=item["severity"],
        file=item["file"],
        line=item.get("line"),
        message=item["message"],
        fix_hint=item.get("fix_hint", ""),
        relevant_memory_id=(
            UUID(item["relevant_memory_id"])
            if item.get("relevant_memory_id") else None
        ),
    )
