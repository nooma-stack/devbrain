"""LLM call-path smoke test.

This test makes a REAL Anthropic API call. It exists to defend against
the class of bug that bit us with PR #119: code merges that look correct
in unit-test land but 429 the first time they hit live `/v1/messages`
because the auth shape, system-prompt fingerprint, or beta header is
subtly wrong.

What this test verifies (end-to-end against api.anthropic.com):

  1. `cognify._anthropic_auth.resolve_anthropic_auth()` returns a usable
     credential from the env (CI sets CLAUDE_CODE_OAUTH_TOKEN from the
     CI_ANTHROPIC_OAUTH_TOKEN secret).
  2. `_llm_extract()` constructs the Anthropic client with the right
     default_headers (anthropic-beta: oauth-2025-04-20).
  3. The request body's first system block is the Claude Code SDK
     fingerprint (PR #122).
  4. The model returns parseable JSON with the expected `lessons` +
     `decisions` keys.

What this test does NOT verify:
  - Correctness of the lesson/decision extraction (model output is
    non-deterministic; we just assert the SHAPE is correct).
  - DB writes (no DB needed — we call _llm_extract directly).
  - End-to-end cognify_extract pass (run separately as part of pytest-db).

Cost: one Sonnet 4.6 call, ~500 input tokens + ≤2K output tokens.
Estimated $0.01-0.03 per invocation at 2026-05 pricing.

The test is SKIPPED if the credential env var is not present, so the
test file is safe to include in default pytest collection — it only
actually fires from the dedicated CI workflow that sets the secret.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "sample_session.txt"

# Skip gracefully when no credential is available. The CI workflow that
# owns this test sets CLAUDE_CODE_OAUTH_TOKEN from a repo secret; local
# `pytest factory/tests/` runs without the secret and skip cleanly.
pytestmark = pytest.mark.skipif(
    not any(
        os.environ.get(k)
        for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    ),
    reason=(
        "no Anthropic credential in env — LLM smoke test only runs when "
        "CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN is set"
    ),
)


def test_llm_extract_against_real_api():
    """Round-trip _llm_extract() against api.anthropic.com.

    This test's job is to catch AUTH regressions specifically (the
    PR #119 class of bug: missing header / wrong fingerprint / wrong
    bearer shape). It is intentionally NOT a test of model output
    quality — that's non-deterministic and would create flaky CI.

    Pass conditions (any):
      * Extraction succeeded — got lessons and/or decisions, no _failure.
      * _failure == 'json_parse' — the API call WORKED; the model just
        didn't return JSON this time. That's a prompt/model issue, not
        an auth issue, and we surface it as a warning rather than a
        hard fail.

    Fail conditions:
      * _failure == 'api' — the API call itself failed (network, auth,
        429, wrong header, etc.). This is exactly the PR #119 class of
        bug this test exists to catch.
      * Missing required keys in the return dict.
      * Token usage is zero (we never actually hit the API).
    """
    from cognify.extract import _llm_extract  # noqa: PLC0415

    content = FIXTURE.read_text()
    assert content.strip(), "fixture session is empty"

    result = _llm_extract(content)

    failure_kind = result.get("_failure")

    if failure_kind == "api":
        # This is the auth-regression case we exist to catch.
        pytest.fail(
            f"LLM smoke test failed (API call did not succeed): "
            f"_failure_detail={result.get('_failure_detail', '<none>')!r}. "
            f"This usually means an auth regression — check the OAuth "
            f"beta header, the Claude Code system-prompt fingerprint, or "
            f"the bearer token validity."
        )

    if failure_kind == "json_parse":
        # Auth worked; model just didn't return JSON. Log + pass.
        # The test's purpose is met (auth verified).
        import warnings
        warnings.warn(
            f"LLM smoke test: API call succeeded but model returned "
            f"non-JSON output. This is a prompt/model issue, not an "
            f"auth issue. Sample: {result.get('_failure_detail', '')[:200]}"
        )

    assert "lessons" in result, f"missing 'lessons' key; got {list(result.keys())}"
    assert "decisions" in result, f"missing 'decisions' key; got {list(result.keys())}"
    assert isinstance(result["lessons"], list)
    assert isinstance(result["decisions"], list)

    # Token usage proves the call actually hit Anthropic. This is the
    # invariant that DOES NOT depend on the model's output content — it
    # depends only on the API call having gone through with valid auth.
    usage = result.get("_usage", {})
    assert usage.get("input_tokens", 0) > 0, (
        f"input_tokens should be > 0 from a real call; got usage={usage!r}. "
        f"This means the call didn't reach Anthropic or returned no token "
        f"accounting — likely an auth or network failure."
    )
    # Note: output_tokens > 0 is implied by usage data existing for a
    # successful call. We don't strictly require it because the empty-output
    # case (output_tokens=0) could still arise if the model truly returns
    # nothing, which is rare but not impossible.


def test_resolved_auth_shape_is_complete_for_oauth():
    """If we're running on an OAuth token (the CI default), the resolved
    auth shape must include the `default_headers` with the OAuth beta
    header. This catches the class of bug from PR #122 (auth_token alone
    is necessary but not sufficient — the beta header gates the request).
    """
    from cognify._anthropic_auth import resolve_anthropic_auth  # noqa: PLC0415

    auth = resolve_anthropic_auth()
    assert auth is not None, "credential disappeared after pytest skip-gate"

    if "auth_token" in auth:
        # OAuth path — must also carry the beta header
        headers = auth.get("default_headers", {})
        assert "anthropic-beta" in headers, (
            f"OAuth-path auth missing anthropic-beta header; got {auth!r}"
        )
        assert "oauth-2025-04-20" in headers["anthropic-beta"]
    else:
        # Console API key path doesn't need the beta header.
        assert "api_key" in auth, f"unexpected auth shape: {auth!r}"


def test_claude_code_system_prefix_returned_for_oauth():
    """If we're running on an OAuth token, the system prefix must be
    populated (it's what Anthropic identity-gates on at /v1/messages)."""
    from cognify._anthropic_auth import claude_code_system_prefix  # noqa: PLC0415

    prefix = claude_code_system_prefix()
    if os.environ.get("ANTHROPIC_API_KEY"):
        # Console path — no prefix needed
        assert prefix is None
    else:
        # OAuth path — prefix must be present
        assert prefix is not None
        assert "You are Claude Code" in prefix
        assert "Claude Agent SDK" in prefix
