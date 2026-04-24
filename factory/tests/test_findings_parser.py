"""Pure-function tests for the JSON findings-block parser and the
JSON-aware count/extract/signature helpers in orchestrator.py.

No DB rows are created — the autouse cleanup fixture used elsewhere
in this directory is unnecessary here.
"""
import json as _json

from orchestrator import (
    _count_blocking,
    _count_warning,
    _extract_blocking_items,
    _extract_warning_items,
    _parse_findings_json,
    _signature_for_finding,
)


def _wrap(json_body: str) -> str:
    """Wrap a JSON body in the fenced block reviewers must produce."""
    return f"Some prose here.\n\n```json findings\n{json_body}\n```\n"


# 1. Well-formed single BLOCKING
def test_well_formed_single_blocking():
    text = _wrap(
        '{"findings": [{"severity": "BLOCKING", '
        '"title": "null deref", "body": "Null check missing at x.py:42", '
        '"file": "x.py", "line": 42}]}'
    )
    assert _count_blocking(text) == 1
    items = _extract_blocking_items(text)
    assert len(items) == 1
    assert "Null check missing" in items[0]
    # JSON path should NOT flag fallback usage.
    count, used_fallback = _count_blocking(text, return_fallback=True)
    assert count == 1
    assert used_fallback is False


# 2. Empty findings list = clean review
def test_empty_findings_list_is_clean():
    text = _wrap('{"findings": []}')
    assert _count_blocking(text) == 0
    assert _count_warning(text) == 0
    _, b_fb = _count_blocking(text, return_fallback=True)
    _, w_fb = _count_warning(text, return_fallback=True)
    assert b_fb is False
    assert w_fb is False


# 3. Multiple mixed severities (2 BLOCKING + 3 WARNING + 1 NIT)
def test_multiple_mixed_severities():
    findings = [
        {"severity": "BLOCKING", "title": "b1", "body": "block-body-one"},
        {"severity": "BLOCKING", "title": "b2", "body": "block-body-two"},
        {"severity": "WARNING", "title": "w1", "body": "warn-body-one"},
        {"severity": "WARNING", "title": "w2", "body": "warn-body-two"},
        {"severity": "WARNING", "title": "w3", "body": "warn-body-three"},
        {"severity": "NIT", "title": "n1", "body": "nit-body-one"},
    ]
    text = _wrap(_json.dumps({"findings": findings}))
    assert _count_blocking(text) == 2
    assert _count_warning(text) == 3
    blockings = _extract_blocking_items(text)
    warnings = _extract_warning_items(text)
    assert blockings == ["block-body-one", "block-body-two"]
    assert warnings == ["warn-body-one", "warn-body-two", "warn-body-three"]


# 4. No JSON block — regex fallback used, flag set
def test_no_json_block_uses_regex_fallback():
    text = "1. WARNING: bare warning at x.py:1\n"
    count, used_fallback = _count_warning(text, return_fallback=True)
    assert count == 1
    assert used_fallback is True
    findings, err = _parse_findings_json(text)
    assert findings is None
    assert err == "no_findings_block"


# 5. Multiple JSON blocks — last one wins
def test_multiple_json_blocks_last_wins():
    first = (
        '```json findings\n{"findings": ['
        '{"severity": "WARNING", "title": "a", "body": "x"},'
        '{"severity": "WARNING", "title": "b", "body": "x"},'
        '{"severity": "WARNING", "title": "c", "body": "x"},'
        '{"severity": "WARNING", "title": "d", "body": "x"},'
        '{"severity": "WARNING", "title": "e", "body": "x"}'
        "]}\n```"
    )
    second = (
        '```json findings\n{"findings": ['
        '{"severity": "WARNING", "title": "z", "body": "x"},'
        '{"severity": "WARNING", "title": "y", "body": "x"}'
        "]}\n```"
    )
    text = f"draft:\n{first}\n\nFINAL:\n{second}\n"
    assert _count_warning(text) == 2  # second block, not first


# 6. Malformed JSON — fallback used, error mentions JSONDecodeError
def test_malformed_json_falls_back():
    text = "```json findings\n{not valid json\n```\n"
    findings, err = _parse_findings_json(text)
    assert findings is None
    assert "JSONDecodeError" in err
    count, used_fallback = _count_warning(text, return_fallback=True)
    assert count == 0
    assert used_fallback is True


# 7. Wrong shape — missing `findings` key
def test_wrong_shape_missing_findings_key():
    text = '```json findings\n{"results": []}\n```\n'
    findings, err = _parse_findings_json(text)
    assert findings is None
    assert err == "missing_findings_key"
    _, used_fallback = _count_warning(text, return_fallback=True)
    assert used_fallback is True


# 8. Invalid severity value — that finding dropped, rest retained, partial flag
def test_invalid_severity_dropped_rest_retained():
    text = _wrap(
        '{"findings": ['
        '{"severity": "URGENT", "title": "x", "body": "x"},'
        '{"severity": "WARNING", "title": "real", "body": "real-body"}'
        "]}"
    )
    findings, err = _parse_findings_json(text)
    assert findings is not None
    assert len(findings) == 1
    assert findings[0]["severity"] == "WARNING"
    assert err is not None
    assert "URGENT" in err
    assert _count_warning(text) == 1


# 9. Case-insensitive severity
def test_case_insensitive_severity():
    text = _wrap(
        '{"findings": ['
        '{"severity": "warning", "title": "x", "body": "y"},'
        '{"severity": "Blocking", "title": "z", "body": "w"}'
        "]}"
    )
    assert _count_warning(text) == 1
    assert _count_blocking(text) == 1


# 10. Signature uses title field, not body truncation
def test_signature_uses_title_field():
    finding = {
        "severity": "WARNING",
        "title": "off-by-one",
        "body": (
            "A long rambling explanation of the off-by-one error "
            "that sprawls across multiple lines and reviewer "
            "paraphrases between rounds "
        ) * 3,
    }
    assert _signature_for_finding(finding) == "off-by-one"

    # Same title, different body → same signature (the point of the contract).
    finding2 = dict(finding)
    finding2["body"] = "completely different wording"
    assert _signature_for_finding(finding) == _signature_for_finding(finding2)

    # Dict without title falls back to body-based signature.
    finding3 = {"severity": "WARNING", "title": None, "body": "Just A Body"}
    assert _signature_for_finding(finding3) == "just a body"

    # Plain string still works (back-compat).
    assert _signature_for_finding("Just A Body") == "just a body"
