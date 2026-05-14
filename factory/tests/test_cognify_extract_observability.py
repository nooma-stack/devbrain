"""Tests for cognify_extract observability changes (A3 + A4):

  A3 — nomenclature: ExtractResult exposes `patterns_created` as an
       alias for `lessons_created` (matching DB `kind='pattern'` storage).
       PassResult.metadata emits both keys.

  A4 — error telemetry: _llm_extract distinguishes "api" failure (the
       API call itself errored) from "json_parse" failure (model returned
       non-JSON) and "empty" (valid JSON but no atoms). Counts surface
       in PassResult.metadata.failure_counts.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import pytest


# ─── A3: ExtractResult.patterns_created alias ────────────────────────────────


def test_extract_result_patterns_alias_matches_lessons():
    from cognify.extract import ExtractResult

    r = ExtractResult(session_id="s1", lessons_created=7, decisions_created=4)
    assert r.lessons_created == 7
    assert r.patterns_created == 7  # alias
    assert r.decisions_created == 4


def test_extract_result_patterns_alias_when_zero():
    from cognify.extract import ExtractResult

    r = ExtractResult(session_id="s1")
    assert r.lessons_created == 0
    assert r.patterns_created == 0


# ─── A4: ExtractResult.failure field ─────────────────────────────────────────


def test_extract_result_failure_defaults_to_none():
    from cognify.extract import ExtractResult

    r = ExtractResult(session_id="s1")
    assert r.failure is None


def test_extract_result_failure_can_be_set():
    from cognify.extract import ExtractResult

    r = ExtractResult(session_id="s1", failure="json_parse")
    assert r.failure == "json_parse"


# ─── A4: _llm_extract failure categorization ─────────────────────────────────


def _stub_anthropic_response(text: str):
    """Build a fake response object shaped like anthropic.types.Message."""
    response = MagicMock()
    response.content = [MagicMock(text=text)]
    response.usage = MagicMock(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return response


def _patch_auth_and_client(response_or_exc):
    """Returns context managers that stub auth + Anthropic client to
    return `response_or_exc`. If exc, the client.messages.create raises;
    otherwise it returns the response."""
    auth_patch = patch(
        "cognify.extract._resolve_auth",
        return_value={"api_key": "sk-ant-api03-FAKE"},
    )

    fake_client = MagicMock()
    if isinstance(response_or_exc, Exception):
        fake_client.messages.create.side_effect = response_or_exc
    else:
        fake_client.messages.create.return_value = response_or_exc

    # Patch the Anthropic constructor where _llm_extract imports it
    # (`import anthropic` inside the function).
    import anthropic
    client_patch = patch.object(anthropic, "Anthropic", return_value=fake_client)
    return auth_patch, client_patch


def test_llm_extract_returns_clean_result_on_valid_json():
    from cognify.extract import _llm_extract

    valid_json = json.dumps({
        "lessons": [{"title": "L1", "content": "body"}],
        "decisions": [{"title": "D1", "content": "body"}],
    })
    auth_p, client_p = _patch_auth_and_client(_stub_anthropic_response(valid_json))
    with auth_p, client_p:
        result = _llm_extract("some content")
    assert result["lessons"] == [{"title": "L1", "content": "body"}]
    assert result["decisions"] == [{"title": "D1", "content": "body"}]
    assert "_failure" not in result  # success path doesn't set _failure


def test_llm_extract_flags_empty_extraction():
    """Valid JSON but no atoms — `_failure='empty'` signals 'LLM succeeded
    but the model didn't find anything worth recording.'"""
    from cognify.extract import _llm_extract

    valid_empty_json = json.dumps({"lessons": [], "decisions": []})
    auth_p, client_p = _patch_auth_and_client(_stub_anthropic_response(valid_empty_json))
    with auth_p, client_p:
        result = _llm_extract("some content")
    assert result["lessons"] == []
    assert result["decisions"] == []
    assert result["_failure"] == "empty"


def test_llm_extract_categorizes_json_parse_failure(caplog):
    """Model returns non-JSON output — categorize as 'json_parse'.
    Usage tokens are preserved (the API call DID succeed, just the
    parsing of its output failed)."""
    from cognify.extract import _llm_extract

    auth_p, client_p = _patch_auth_and_client(_stub_anthropic_response("Not JSON at all!"))
    with caplog.at_level(logging.WARNING, logger="cognify.extract"):
        with auth_p, client_p:
            result = _llm_extract("content")

    assert result["lessons"] == []
    assert result["decisions"] == []
    assert result["_failure"] == "json_parse"
    assert "_failure_detail" in result
    # Token usage from the (successful) API call is preserved
    assert result["_usage"]["input_tokens"] == 10
    assert result["_usage"]["output_tokens"] == 5
    # The payload sample is logged for diagnostics
    assert any("JSON parse failed" in rec.message for rec in caplog.records)


def test_llm_extract_categorizes_api_call_failure():
    """API call itself raises — categorize as 'api'. No usage to preserve."""
    from cognify.extract import _llm_extract

    auth_p, client_p = _patch_auth_and_client(RuntimeError("connection refused"))
    with auth_p, client_p:
        result = _llm_extract("content")

    assert result["lessons"] == []
    assert result["decisions"] == []
    assert result["_failure"] == "api"
    assert "connection refused" in result["_failure_detail"]
    assert result["_usage"]["input_tokens"] == 0  # _empty_usage


def test_llm_extract_strips_markdown_fences_before_parsing():
    """Some models wrap JSON in ```json ... ``` fences; we strip them."""
    from cognify.extract import _llm_extract

    fenced = "```json\n" + json.dumps({"lessons": [], "decisions": [{"title": "D1", "content": "x"}]}) + "\n```"
    auth_p, client_p = _patch_auth_and_client(_stub_anthropic_response(fenced))
    with auth_p, client_p:
        result = _llm_extract("content")
    assert result["decisions"] == [{"title": "D1", "content": "x"}]
    # Fenced+valid JSON with one atom → no failure flag (decisions is non-empty)
    assert "_failure" not in result


# ─── APIConnectionError retry-with-backoff (2026-05-14 incident fix) ─────────


def test_llm_extract_retries_on_api_connection_error_and_succeeds(monkeypatch):
    """If the first call raises APIConnectionError but the second
    succeeds, we get a clean result with no _failure flag. The client
    should have been recycled (close+new) between attempts."""
    import anthropic
    from cognify.extract import _llm_extract

    # Bypass real time.sleep so the test runs fast.
    monkeypatch.setattr("cognify.extract.time.sleep", lambda *_a, **_kw: None)

    valid_json = json.dumps({
        "lessons": [{"title": "L", "content": "x"}],
        "decisions": [],
    })

    # First call raises, second returns the success response.
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [
        anthropic.APIConnectionError(request=None),
        _stub_anthropic_response(valid_json),
    ]

    auth_patch = patch(
        "cognify.extract._resolve_auth",
        return_value={"api_key": "sk-ant-api03-FAKE"},
    )

    construct_count = {"n": 0}

    def _fake_constructor(*args, **kwargs):
        construct_count["n"] += 1
        return fake_client

    construct_patch = patch.object(anthropic, "Anthropic", side_effect=_fake_constructor)

    with auth_patch, construct_patch:
        result = _llm_extract("content")

    # Extraction succeeded
    assert "_failure" not in result, f"got: {result}"
    assert result["lessons"] == [{"title": "L", "content": "x"}]
    # Two API calls attempted (1 failed + 1 succeeded)
    assert fake_client.messages.create.call_count == 2
    # Client should have been recycled — at least 2 constructions
    # (initial + at least 1 retry recycle)
    assert construct_count["n"] >= 2
    # The stale client's close() should have been called during recycle
    assert fake_client.close.called


def test_llm_extract_gives_up_after_max_retries_on_persistent_connection_error(monkeypatch):
    """If APIConnectionError persists across all retries, return
    _failure='api'. Test that we don't retry forever."""
    import anthropic
    from cognify.extract import _API_MAX_RETRIES, _llm_extract

    monkeypatch.setattr("cognify.extract.time.sleep", lambda *_a, **_kw: None)

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = anthropic.APIConnectionError(request=None)

    auth_patch = patch(
        "cognify.extract._resolve_auth",
        return_value={"api_key": "sk-ant-api03-FAKE"},
    )
    construct_patch = patch.object(anthropic, "Anthropic", return_value=fake_client)

    with auth_patch, construct_patch:
        result = _llm_extract("content")

    assert result.get("_failure") == "api"
    assert result["lessons"] == []
    # Used all retries — call count equals _API_MAX_RETRIES exactly
    assert fake_client.messages.create.call_count == _API_MAX_RETRIES


def test_llm_extract_does_not_retry_non_connection_errors(monkeypatch):
    """A 401 / 400 / random RuntimeError doesn't get retried — those
    won't get better from waiting, and retrying just wastes time."""
    from cognify.extract import _llm_extract

    monkeypatch.setattr("cognify.extract.time.sleep", lambda *_a, **_kw: None)

    fake_client = MagicMock()
    fake_client.messages.create.side_effect = RuntimeError("401 Unauthorized")

    auth_patch = patch(
        "cognify.extract._resolve_auth",
        return_value={"api_key": "sk-ant-api03-FAKE"},
    )
    import anthropic
    construct_patch = patch.object(anthropic, "Anthropic", return_value=fake_client)

    with auth_patch, construct_patch:
        result = _llm_extract("content")

    assert result.get("_failure") == "api"
    # Single attempt only — non-connection errors don't retry
    assert fake_client.messages.create.call_count == 1
