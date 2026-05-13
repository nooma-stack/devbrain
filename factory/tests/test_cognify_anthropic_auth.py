"""Tests for cognify._anthropic_auth.

Resolution order:
  1. ANTHROPIC_API_KEY (Console key, X-Api-Key header → `api_key=`)
  2. CLAUDE_CODE_OAUTH_TOKEN (subscription OAuth, Bearer → `auth_token=`)
  3. ANTHROPIC_AUTH_TOKEN (same shape as 2, Anthropic-docs name)
  4. None → caller skips LLM work gracefully

OAuth path additionally carries the `anthropic-beta: oauth-2025-04-20`
default header and requires callers to prepend the Claude Code SDK
fingerprint to their system prompt (see `claude_code_system_prefix`).
"""
from __future__ import annotations

import os
from unittest.mock import patch

from cognify._anthropic_auth import (
    claude_code_system_prefix,
    resolve_anthropic_auth,
)

_OAUTH_BETA = {"anthropic-beta": "oauth-2025-04-20"}
_OAUTH_PREFIX = (
    "You are Claude Code, Anthropic's official CLI for Claude, "
    "running within the Claude Agent SDK."
)


def _isolated_env(**vars):
    """Strip the target env vars to start clean, then layer in `vars`."""
    base = {k: v for k, v in os.environ.items()
            if k not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")}
    base.update(vars)
    return base


def test_returns_none_when_no_credential():
    with patch.dict(os.environ, _isolated_env(), clear=True):
        assert resolve_anthropic_auth() is None


def test_console_api_key_routes_to_api_key_kwarg():
    with patch.dict(os.environ, _isolated_env(ANTHROPIC_API_KEY="sk-ant-api03-FAKE"), clear=True):
        assert resolve_anthropic_auth() == {"api_key": "sk-ant-api03-FAKE"}


def test_oauth_token_routes_to_auth_token_kwarg_with_beta_header():
    with patch.dict(os.environ, _isolated_env(CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat1-FAKE"), clear=True):
        assert resolve_anthropic_auth() == {
            "auth_token": "sk-ant-oat1-FAKE",
            "default_headers": _OAUTH_BETA,
        }


def test_anthropic_auth_token_also_routes_to_auth_token_with_beta_header():
    with patch.dict(os.environ, _isolated_env(ANTHROPIC_AUTH_TOKEN="sk-ant-oat1-OTHER"), clear=True):
        assert resolve_anthropic_auth() == {
            "auth_token": "sk-ant-oat1-OTHER",
            "default_headers": _OAUTH_BETA,
        }


def test_console_key_wins_over_oauth_when_both_set():
    """If both shapes are configured, prefer Console (deterministic billing)."""
    env = _isolated_env(
        ANTHROPIC_API_KEY="sk-ant-api03-WINS",
        CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat1-LOSES",
    )
    with patch.dict(os.environ, env, clear=True):
        result = resolve_anthropic_auth()
        assert result == {"api_key": "sk-ant-api03-WINS"}
        assert "auth_token" not in result
        assert "default_headers" not in result


def test_claude_code_token_wins_over_generic_auth_token():
    """When both bearer-style envs are set, prefer the Claude Code one
    (the documented Claude Code env var name; more idiomatic)."""
    env = _isolated_env(
        CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat1-FROM-CLAUDE-CODE",
        ANTHROPIC_AUTH_TOKEN="sk-ant-oat1-FROM-GENERIC",
    )
    with patch.dict(os.environ, env, clear=True):
        assert resolve_anthropic_auth() == {
            "auth_token": "sk-ant-oat1-FROM-CLAUDE-CODE",
            "default_headers": _OAUTH_BETA,
        }


def test_empty_string_env_treated_as_unset():
    """An empty string for ANTHROPIC_API_KEY should fall through to OAuth, not
    return {"api_key": ""} which the SDK would reject."""
    env = _isolated_env(
        ANTHROPIC_API_KEY="",
        CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat1-USE-ME",
    )
    with patch.dict(os.environ, env, clear=True):
        assert resolve_anthropic_auth() == {
            "auth_token": "sk-ant-oat1-USE-ME",
            "default_headers": _OAUTH_BETA,
        }


# ── claude_code_system_prefix ────────────────────────────────────────────────


def test_system_prefix_none_when_no_credential():
    with patch.dict(os.environ, _isolated_env(), clear=True):
        assert claude_code_system_prefix() is None


def test_system_prefix_none_for_console_api_key():
    """Console keys don't need the fingerprint — the SDK X-Api-Key path is
    not gated on the system-prompt fingerprint."""
    with patch.dict(os.environ, _isolated_env(ANTHROPIC_API_KEY="sk-ant-api03-FAKE"), clear=True):
        assert claude_code_system_prefix() is None


def test_system_prefix_present_for_oauth_token():
    with patch.dict(os.environ, _isolated_env(CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat1-FAKE"), clear=True):
        assert claude_code_system_prefix() == _OAUTH_PREFIX


def test_system_prefix_present_for_anthropic_auth_token():
    with patch.dict(os.environ, _isolated_env(ANTHROPIC_AUTH_TOKEN="sk-ant-oat1-OTHER"), clear=True):
        assert claude_code_system_prefix() == _OAUTH_PREFIX


def test_system_prefix_none_when_console_wins_over_oauth():
    """Console key takes precedence as the active credential, so no prefix
    is needed — matches resolve_anthropic_auth() returning the api_key shape."""
    env = _isolated_env(
        ANTHROPIC_API_KEY="sk-ant-api03-WINS",
        CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat1-LOSES",
    )
    with patch.dict(os.environ, env, clear=True):
        assert claude_code_system_prefix() is None
