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


# 5. Multiple JSON blocks — rejected, regex fallback fires (PR #36).
# Prior behavior ("last block wins") opened a diff-echo attack path
# where a reviewer's real findings could be silenced by any later
# fenced block — including diff context a reviewer pasted in. The
# stricter contract rejects >1 blocks with a count-bearing error so
# the existing regex fallback + reviewer_output_malformed flag
# (orchestrator.py _run_review) fires automatically.
def test_multiple_json_blocks_rejects_and_falls_back():
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
    findings, err = _parse_findings_json(text)
    assert findings is None
    assert err == "multiple_findings_blocks:2"
    _, used_fallback = _count_warning(text, return_fallback=True)
    assert used_fallback is True


# 5b. Three blocks also rejected — locks in that rejection is not a
# 2-block special case.
def test_three_json_blocks_also_rejected():
    block = (
        '```json findings\n{"findings": ['
        '{"severity": "WARNING", "title": "t", "body": "x"}'
        "]}\n```"
    )
    text = f"round 1:\n{block}\n\nround 2:\n{block}\n\nround 3:\n{block}\n"
    findings, err = _parse_findings_json(text)
    assert findings is None
    assert err == "multiple_findings_blocks:3"


# 5c. Diff-echo attack: a real BLOCKING block is followed by reviewer
# prose that quotes diff context containing a benign `{"findings": []}`
# block. Under "last block wins" the BLOCKING would be silenced. Under
# PR #36 the parser rejects both blocks and the regex fallback rescues
# the real finding from the prose.
def test_diff_echo_attack_does_not_suppress_findings():
    real_block = (
        '```json findings\n{"findings": ['
        '{"severity": "BLOCKING", "title": "sql injection", '
        '"body": "unescaped input at db.py:42"}'
        "]}\n```"
    )
    echoed_block = '```json findings\n{"findings": []}\n```'
    text = (
        "I found a BLOCKING issue. See below.\n\n"
        f"{real_block}\n\n"
        "For context, here is the diff the implementer pasted back:\n"
        f"    {echoed_block}\n"
    )
    findings, err = _parse_findings_json(text)
    assert findings is None
    assert err == "multiple_findings_blocks:2"

    # Defense-in-depth: when the prose also carries a stacked-prefix
    # BLOCKING marker, the regex fallback still sees the real finding.
    text_with_prose_marker = (
        "1. BLOCKING: sql injection — unescaped input at db.py:42\n\n"
        f"{real_block}\n\n"
        "Diff context follows:\n"
        f"    {echoed_block}\n"
    )
    count, used_fallback = _count_blocking(
        text_with_prose_marker, return_fallback=True
    )
    assert used_fallback is True
    assert count == 1


# 5d. Diff-echo-via-rubric: reviewer quotes the severity rubric from
# the prompt (lines of the form `- BLOCKING → ...`) AND emits two JSON
# blocks (forcing multi-block rejection → regex fallback). Without the
# `(?!\s*→)` negative lookahead added in PR #37, the fallback would
# count each echoed rubric line as a real finding and incorrectly
# route the job to fix-loop.
def test_rubric_echo_with_two_blocks_is_not_miscounted():
    text = (
        "# My review\n\n"
        "Severity rubric (echoed from prompt):\n"
        "- BLOCKING → actual vulnerability, PHI exposure, missing auth check\n"
        "- WARNING  → defense-in-depth suggestion, narrow input gap\n"
        "- NIT      → best-practice suggestion\n\n"
        "## Findings\n\n"
        '```json findings\n{"findings": []}\n```\n\n'
        "## Draft (forgot to delete)\n\n"
        '```json findings\n{"findings": []}\n```\n'
    )
    # Two blocks → multi-block rejection → regex fallback.
    findings, err = _parse_findings_json(text)
    assert findings is None
    assert err.startswith("multiple_findings_blocks")
    # Fallback must not count the echoed rubric lines.
    b_count, b_fb = _count_blocking(text, return_fallback=True)
    w_count, w_fb = _count_warning(text, return_fallback=True)
    assert b_fb is True and w_fb is True
    assert b_count == 0
    assert w_count == 0
    assert _extract_blocking_items(text) == []
    assert _extract_warning_items(text) == []


# 5e. Sanity check: a real `- BLOCKING: ...` finding in the prose
# (the shape reviewers actually produce when they fall back to
# regex) must still be counted by the fallback. The negative
# lookahead only excludes the `→` rubric form.
def test_real_dash_prefixed_findings_still_counted_by_fallback():
    text = (
        "- BLOCKING: null deref at x.py:42\n"
        "- WARNING: suboptimal pattern at y.py:10\n"
    )
    assert _count_blocking(text) == 1
    assert _count_warning(text) == 1
    blockings = _extract_blocking_items(text)
    warnings = _extract_warning_items(text)
    assert len(blockings) == 1 and "null deref" in blockings[0]
    assert len(warnings) == 1 and "suboptimal pattern" in warnings[0]


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
