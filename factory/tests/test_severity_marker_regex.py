"""Tests for stacked-prefix severity markers in _count_*/_extract_* helpers.

The four helpers in orchestrator.py
(_count_blocking, _count_warning, _extract_blocking_items,
_extract_warning_items) used to use a prefix group that matched AT MOST
ONE of {number, bold, dash}. Reviewers naturally produce stacked
prefixes like ``**1. WARNING — text**`` or ``- **WARNING:** body``,
which the old regex silently missed — leaving warning_count at 0 on
real findings and skipping the WARNING fix-loop gate.

These are pure-function tests (no DB fixture); they exercise the regex
behavior directly. The autouse cleanup fixture used elsewhere in this
directory is unnecessary here because no factory_jobs rows are created.
"""
from orchestrator import (
    _count_blocking,
    _count_warning,
    _extract_blocking_items,
    _extract_warning_items,
)


def test_stacked_bold_number_warning_regression():
    """``**1. WARNING — text**`` was the original miss that motivated the fix."""
    text = "**1. WARNING — missing null check at x.py:42**"
    assert _count_warning(text) == 1


def test_stacked_bold_number_blocking_and_warning_mixed():
    """Mixed bold-number stacked forms: 1 BLOCKING + 1 WARNING."""
    text = (
        "**2. BLOCKING — null deref at a.py:10**\n"
        "**3. WARNING — suboptimal pattern at b.py:5**\n"
    )
    assert _count_blocking(text) == 1
    assert _count_warning(text) == 1


def test_number_then_bold_warning():
    """``1. **WARNING:** body`` — number first, then bold."""
    text = "1. **WARNING:** missing docstring"
    assert _count_warning(text) == 1


def test_dash_then_bold_warning():
    """``- **WARNING:** body`` — dash first, then bold."""
    text = "- **WARNING:** consider caching result"
    assert _count_warning(text) == 1


def test_plain_forms_regression_guard():
    """Bare / numbered / bolded / dashed forms must still be detected.

    Locks in the original behavior so the {0,4} change doesn't regress
    the simple cases the prior regex already handled.
    """
    text = (
        "WARNING: bare form\n"
        "1. WARNING: numbered\n"
        "**WARNING**: bolded\n"
        "- WARNING: dashed\n"
    )
    assert _count_warning(text) == 4

    btext = (
        "BLOCKING: bare\n"
        "1. BLOCKING: numbered\n"
        "**BLOCKING**: bolded\n"
        "- BLOCKING: dashed\n"
    )
    assert _count_blocking(btext) == 4


def test_prose_word_warning_does_not_match():
    """A prose mention like ``warning sign`` must not be counted.

    The start-of-line/list anchor is what keeps mid-sentence words out.
    """
    text = "There was a warning sign on the wall."
    assert _count_warning(text) == 0


def test_realistic_review_body_mixed_styles():
    """A realistic reviewer body with 2 BLOCKING + 3 WARNING + 1 NIT.

    Mixes plain numbered, bold-stacked, and dash-stacked styles so the
    helpers exercise the full prefix permutation set in one body.
    Verifies both counts AND that extracted bodies stop at the next
    severity boundary.
    """
    text = (
        "## Review\n"
        "1. BLOCKING: missing auth check at handler.py:88\n"
        "**2. WARNING — suboptimal query plan at db.py:120**\n"
        "- **WARNING:** docstring missing for public API foo()\n"
        "3. WARNING: hardcoded timeout at worker.py:45\n"
        "**4. BLOCKING — race condition at cache.py:200**\n"
        "5. NIT: prefer f-strings\n"
    )
    assert _count_blocking(text) == 2
    assert _count_warning(text) == 3

    blockings = _extract_blocking_items(text)
    assert len(blockings) == 2
    assert "missing auth check" in blockings[0]
    # First blocking body must stop before the next WARNING
    assert "suboptimal query plan" not in blockings[0]
    assert "race condition" in blockings[1]

    warnings = _extract_warning_items(text)
    assert len(warnings) == 3
    assert "suboptimal query plan" in warnings[0]
    assert "docstring missing" in warnings[1]
    assert "hardcoded timeout" in warnings[2]
    # Last warning body must not bleed into the BLOCKING that follows
    assert "race condition" not in warnings[2]


def test_extract_warning_stops_at_stacked_blocking_boundary():
    """End-boundary regex must also tolerate stacked prefixes.

    ``"1. WARNING: foo\\n\\n**2. BLOCKING: bar**"`` — the warning body
    should stop before the bold-stacked BLOCKING, otherwise the
    extracted text leaks into the next finding.
    """
    text = "1. WARNING: foo\n\n**2. BLOCKING: bar**"
    warnings = _extract_warning_items(text)
    assert len(warnings) == 1
    assert "foo" in warnings[0]
    assert "BLOCKING" not in warnings[0]
    assert "bar" not in warnings[0]


def test_bound_check_six_dashes_does_not_match():
    """Locks the {0,4} cap — beyond 4 stacked prefixes we deliberately
    don't match. This guards against a future change that loosens the
    bound and re-introduces the catastrophic-backtracking risk that {0,4}
    was chosen to avoid.
    """
    text = ("- " * 6) + "WARNING: body"
    assert _count_warning(text) == 0
