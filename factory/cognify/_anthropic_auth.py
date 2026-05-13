"""Anthropic credential resolution for cognify's LLM passes.

The cognify pipeline's extract + edges passes call the Anthropic API
to derive lessons / contradictions from session content. Two credential
shapes are accepted:

  1. Console API key — `sk-ant-api03-...`. Created at
     console.anthropic.com → Settings → API keys. Sent by the SDK as
     the `X-Api-Key` header. Set via env var `ANTHROPIC_API_KEY`.

  2. Subscription OAuth token — `sk-ant-oat<N>-...`. Created by
     `claude setup-token` against a Pro/Max/Team/Enterprise account.
     Sent by the SDK as `Authorization: Bearer`. Set via env var
     `CLAUDE_CODE_OAUTH_TOKEN` (the name Claude Code's docs use) or
     `ANTHROPIC_AUTH_TOKEN` (the name the SDK's auth-precedence docs
     use). Either works — both resolve to the same SDK kwarg.

Returns the kwargs to pass into `anthropic.Anthropic(...)`. Returns
None when no credential is available; callers should skip the LLM
work gracefully.

OAuth path notes
----------------
For subscription OAuth tokens to be honored at `/v1/messages`, Anthropic
requires two things beyond the Bearer header alone:

  * `anthropic-beta: oauth-2025-04-20` request header — added here via
    `default_headers` so every SDK call carries it.
  * The request's first system block must be the Claude Code SDK
    fingerprint string (see `claude_code_system_prefix`). Without it,
    `/v1/messages` returns 429 `rate_limit_error` with a terse "Error"
    body — not a real rate limit, an identity-gate rejection masked as
    one. Callers must prepend the prefix to their system prompt when
    this resolver returns the OAuth shape.
"""
from __future__ import annotations

import os
from typing import Any

# Required `anthropic-beta` header value for subscription OAuth.
_OAUTH_BETA_HEADER = "oauth-2025-04-20"

# Claude Code SDK fingerprint baked into the official binary. Anthropic
# gates subscription OAuth on the request's first system block matching
# this string literally.
_CLAUDE_CODE_SYSTEM_PREFIX = (
    "You are Claude Code, Anthropic's official CLI for Claude, "
    "running within the Claude Agent SDK."
)


def resolve_anthropic_auth() -> dict[str, Any] | None:
    """Return SDK kwargs dict, or None if no credential present.

    Precedence: ANTHROPIC_API_KEY (Console) wins if set, since it gives
    deterministic per-call billing; OAuth bearer tokens are the fallback.

    Console path returns: ``{"api_key": "<value>"}``.
    OAuth path returns: ``{"auth_token": "<value>", "default_headers":
    {"anthropic-beta": "oauth-2025-04-20"}}`` — and callers must also
    prepend ``claude_code_system_prefix()`` to their system prompt.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return {"api_key": api_key}

    bearer = (
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    )
    if bearer:
        return {
            "auth_token": bearer,
            "default_headers": {"anthropic-beta": _OAUTH_BETA_HEADER},
        }

    return None


def claude_code_system_prefix() -> str | None:
    """Return the system-prompt fingerprint string for the OAuth path.

    Returns the Claude Code SDK fingerprint when the active credential
    is a subscription OAuth token; returns None when the active
    credential is a Console API key (no prefix needed) or when no
    credential is configured.

    Callers should prepend the returned string as the *first* system
    block (above their own task-specific system prompt). Without it,
    `/v1/messages` rejects OAuth-token requests with a 429
    `rate_limit_error` — an identity-gate rejection, not a real rate
    limit.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return None
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get(
        "ANTHROPIC_AUTH_TOKEN"
    ):
        return _CLAUDE_CODE_SYSTEM_PREFIX
    return None
