"""Tests for `factory/cognify/extract.py:_parse_json_with_fallbacks`.

The 2026-05-17 brightbot cognify-bulk retry revealed that ~226
sessions failed `json_parse` because Sonnet sometimes abandons the
JSON task and returns conversational text, code snippets, or input
echoes. The fix has two parts:

  1. Assistant prefill (`{`) — forces Sonnet to continue as JSON.
     Implemented in extract.py's messages.create() call.
  2. Fallback parser — for the remaining edge cases where Sonnet
     emits valid JSON wrapped in markdown fences or embedded in
     explanatory prose. Tested here.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Add factory/ to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cognify.extract import _parse_json_with_fallbacks  # noqa: E402


def test_direct_parse_happy_path():
    text = '{"lessons": [], "decisions": [{"title": "x", "content": "y"}]}'
    assert _parse_json_with_fallbacks(text) == {
        "lessons": [],
        "decisions": [{"title": "x", "content": "y"}],
    }


def test_strips_json_fenced_code_block():
    """Sonnet sometimes preambles with `Here's the breakdown:` then a
    fenced block. The helper finds the block and parses it."""
    text = (
        "Here's a breakdown of the lessons and decisions:\n\n"
        "```json\n"
        '{\n  "lessons": [{"title": "A", "content": "B"}],\n'
        '  "decisions": []\n}\n'
        "```\n\nLet me know if you need more detail."
    )
    result = _parse_json_with_fallbacks(text)
    assert result is not None
    assert result["lessons"] == [{"title": "A", "content": "B"}]
    assert result["decisions"] == []


def test_strips_plain_fenced_code_block_no_lang():
    """``` without `json` language tag still parses."""
    text = '```\n{"lessons": [], "decisions": []}\n```'
    assert _parse_json_with_fallbacks(text) == {
        "lessons": [], "decisions": [],
    }


def test_extracts_embedded_json_object():
    """When the model embeds JSON in the middle of prose, find the
    outer {…} braces and try parsing the substring."""
    text = (
        "I'll extract these now. The result is "
        '{"lessons": [{"title": "T1", "content": "C1"}], "decisions": []} '
        "and I hope that's what you wanted."
    )
    result = _parse_json_with_fallbacks(text)
    assert result is not None
    assert result["lessons"][0]["title"] == "T1"


def test_returns_none_for_pure_garbage():
    """Sonnet completely abandoning the task — returning conversational
    text with no JSON anywhere — returns None and the caller falls
    back to the json_parse failure path."""
    text = (
        "Let me check on the current state and the Google groups "
        "tooling available. I'd be happy to help once I have more "
        "context."
    )
    assert _parse_json_with_fallbacks(text) is None


def test_returns_none_for_empty_input():
    assert _parse_json_with_fallbacks("") is None
    assert _parse_json_with_fallbacks("   \n   ") is None


def test_prefill_reattachment_case():
    """When extract.py prepends `{` because the prefilled brace was
    omitted from the API response, the resulting text starts with `{`
    and parses directly."""
    # This is what extract.py builds: prefill `{` + Sonnet's continuation
    reconstructed = '{"lessons": [{"title": "X", "content": "Y"}], "decisions": []}'
    result = _parse_json_with_fallbacks(reconstructed)
    assert result is not None
    assert result["lessons"][0]["title"] == "X"


def test_fence_extraction_preferred_when_top_level_parse_fails():
    """Strategy 1 fails (preamble before the brace), strategy 2 (fence)
    succeeds before falling to strategy 3 (substring scan)."""
    text = (
        'Note: the output may contain a list. Here is the data:\n'
        '```json\n'
        '{"lessons": [], "decisions": []}\n'
        '```\n'
    )
    result = _parse_json_with_fallbacks(text)
    assert result == {"lessons": [], "decisions": []}
