"""Anthropic credential resolution for cognify's LLM passes.

The cognify pipeline's extract + edges passes call the Anthropic API
to derive lessons / contradictions from session content. Three credential
shapes are accepted:

  1. Console API key — `sk-ant-api03-...`. Created at
     console.anthropic.com → Settings → API keys. Sent by the SDK as
     the `X-Api-Key` header. Set via env var `ANTHROPIC_API_KEY`.

  2. Subscription OAuth token (dev's, per-session) — `sk-ant-oat<N>-...`.
     Created by `claude setup-token` against a Pro/Max/Team/Enterprise
     account. Sent by the SDK as `Authorization: Bearer`. Set via env
     var `CLAUDE_CODE_OAUTH_TOKEN` (the name Claude Code's docs use).
     This is the slot that carries the triggering dev's token when
     cognify is invoked from `end_session` — the dev's session env has
     their personal token, so cognify burns the dev's Max budget for
     work they triggered. Per-dev attribution.

  3. Subscription OAuth token (system fallback) —
     `DEVBRAIN_COGNIFY_OAUTH_TOKEN`. The admin's token. Used when no
     dev is present (e.g., scheduled launchd cognify-extract runs),
     where the system token should pay. Lower precedence than the
     dev's token in (2), so dev-triggered runs still attribute to
     the dev — this only kicks in when (2) is unset.

  Legacy: `ANTHROPIC_AUTH_TOKEN` is also accepted (the name the
  Anthropic SDK's auth-precedence docs use). Lowest precedence.

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


def _resolve_oauth_token() -> str | None:
    """Pick the OAuth token from env with the documented precedence.

    Order (each one wins over later entries):
      1. ``CLAUDE_CODE_OAUTH_TOKEN``         — dev's session token
      2. ``DEVBRAIN_COGNIFY_OAUTH_TOKEN``    — admin/system fallback
      3. ``ANTHROPIC_AUTH_TOKEN``            — legacy SDK-docs name

    The split between (1) and (2) is the per-dev attribution lever: a
    dev-triggered cognify run (from `end_session`) finds the dev's token
    in slot (1) and burns the dev's Max budget. A launchd-scheduled run
    has no dev present, so (1) is unset and (2) kicks in — Patrick's
    system token pays.
    """
    return (
        os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        or os.environ.get("DEVBRAIN_COGNIFY_OAUTH_TOKEN")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    )


def resolve_anthropic_auth() -> dict[str, Any] | None:
    """Return SDK kwargs dict, or None if no credential present.

    Precedence: ANTHROPIC_API_KEY (Console) wins if set, since it gives
    deterministic per-call billing; OAuth bearer tokens are the fallback,
    with their own internal precedence (see `_resolve_oauth_token`).

    Console path returns: ``{"api_key": "<value>"}``.
    OAuth path returns: ``{"auth_token": "<value>", "default_headers":
    {"anthropic-beta": "oauth-2025-04-20"}}`` — and callers must also
    prepend ``claude_code_system_prefix()`` to their system prompt.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return {"api_key": api_key}

    bearer = _resolve_oauth_token()
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
    if _resolve_oauth_token():
        return _CLAUDE_CODE_SYSTEM_PREFIX
    return None
