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
    _findings_overlap,
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


# 10. Signature set includes title and body so JSON↔regex-fallback mix still matches
def test_signature_includes_title_and_body_for_cross_path_matching():
    """Post-PR #34 contract: `_signature_for_finding` returns a
    FROZENSET of 1 or 2 signatures (title + body) so two findings
    match when they share ANY signature — not when their full sets
    are equal. Solves the JSON-round-vs-regex-round asymmetry flagged
    in the arch review of job a51efc39."""
    finding = {
        "severity": "WARNING",
        "title": "off-by-one",
        "body": "A long rambling explanation of the off-by-one error "
                "that sprawls across multiple lines",
    }
    sigs = _signature_for_finding(finding)
    assert isinstance(sigs, frozenset)
    assert "off-by-one" in sigs
    # Body signature is also present (80-char lowercased prefix).
    assert any("off-by-one error" in s for s in sigs)

    # Same title, different body → sig SETS overlap (share title) but
    # are not equal (bodies differ). Overlap is the matching contract.
    finding2 = dict(finding)
    finding2["body"] = "completely different wording here"
    sigs2 = _signature_for_finding(finding2)
    assert sigs & sigs2  # non-empty intersection — will match in oscillation
    assert sigs != sigs2  # not identical — body sigs differ

    # Dict with None title → only body-sig in the set.
    finding3 = {"severity": "WARNING", "title": None, "body": "Just A Body"}
    sigs3 = _signature_for_finding(finding3)
    assert sigs3 == frozenset({"just a body"})

    # Plain string → single body-sig, back-compat.
    assert _signature_for_finding("Just A Body") == frozenset({"just a body"})


# 11. _findings_overlap matches across JSON↔regex-fallback round boundary
def test_findings_overlap_bridges_json_and_regex_rounds():
    """Round 1 emits JSON with title+body; round 2 reviewer ignores
    the contract and falls back to regex (title=None, body=extracted
    text). If both rounds reference the same issue via shared body
    prefix, the guardrail should still see the repeat. This is the
    arch-review WARNING on job a51efc39 — before fix, these two
    signature paths were incomparable and the repeat was missed."""
    # Shared body prefix long enough for the 80-char body-sig to match.
    shared_body = (
        "The increment at line 42 skips the last element of the array, "
        "causing an off-by-one error in the iterator consumer downstream"
    )
    prior_json = [{
        "severity": "WARNING", "title": "off-by-one",
        "body": shared_body,
    }]
    # Same issue, but a regex-fallback synthetic dict (title=None,
    # body carries the same extracted text).
    current_fallback = [{
        "severity": "WARNING", "title": None,
        "body": shared_body + " (reviewer added extra commentary)",
    }]
    matches = _findings_overlap(current_fallback, prior_json)
    assert len(matches) == 1


# 12. Non-string title/body dropped with partial-parse flag
def test_non_string_title_or_body_is_dropped():
    """Addresses the security-review NIT on job a51efc39: a reviewer
    emitting `{"title": {...}}` or `{"body": 42}` used to pass
    validation and then crash `_signature_for_finding`'s `.strip()`
    deeper in the pipeline. Parser now drops these findings at
    normalization time, keeps the valid ones, and flags the artifact
    via the partial-parse error detail."""
    text = _wrap(_json.dumps({"findings": [
        {"severity": "WARNING", "title": {"not": "a string"},
         "body": "has a body"},
        {"severity": "WARNING", "title": "ok", "body": 42},
        {"severity": "WARNING", "title": "real", "body": "real-body"},
    ]}))
    findings, err = _parse_findings_json(text)
    assert findings is not None
    assert len(findings) == 1
    assert findings[0]["title"] == "real"
    assert err is not None
    assert "non_string_title" in err or "non_string_body" in err
