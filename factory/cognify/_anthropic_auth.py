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
"""
from __future__ import annotations

import os


def resolve_anthropic_auth() -> dict[str, str] | None:
    """Return SDK kwargs dict, or None if no credential present.

    Precedence: ANTHROPIC_API_KEY (Console) wins if set, since it gives
    deterministic per-call billing; OAuth bearer tokens are the fallback.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return {"api_key": api_key}

    bearer = (
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    )
    if bearer:
        return {"auth_token": bearer}

    return None
