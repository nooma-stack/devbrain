"""Tests for cognify._anthropic_auth.

Resolution order:
  1. ANTHROPIC_API_KEY (Console key, X-Api-Key header → `api_key=`)
  2. CLAUDE_CODE_OAUTH_TOKEN (subscription OAuth, Bearer → `auth_token=`)
  3. ANTHROPIC_AUTH_TOKEN (same shape as 2, Anthropic-docs name)
  4. None → caller skips LLM work gracefully
"""
from __future__ import annotations

import os
from unittest.mock import patch

from cognify._anthropic_auth import resolve_anthropic_auth


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


def test_oauth_token_routes_to_auth_token_kwarg():
    with patch.dict(os.environ, _isolated_env(CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat1-FAKE"), clear=True):
        assert resolve_anthropic_auth() == {"auth_token": "sk-ant-oat1-FAKE"}


def test_anthropic_auth_token_also_routes_to_auth_token():
    with patch.dict(os.environ, _isolated_env(ANTHROPIC_AUTH_TOKEN="sk-ant-oat1-OTHER"), clear=True):
        assert resolve_anthropic_auth() == {"auth_token": "sk-ant-oat1-OTHER"}


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


def test_claude_code_token_wins_over_generic_auth_token():
    """When both bearer-style envs are set, prefer the Claude Code one
    (the documented Claude Code env var name; more idiomatic)."""
    env = _isolated_env(
        CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat1-FROM-CLAUDE-CODE",
        ANTHROPIC_AUTH_TOKEN="sk-ant-oat1-FROM-GENERIC",
    )
    with patch.dict(os.environ, env, clear=True):
        assert resolve_anthropic_auth() == {"auth_token": "sk-ant-oat1-FROM-CLAUDE-CODE"}


def test_empty_string_env_treated_as_unset():
    """An empty string for ANTHROPIC_API_KEY should fall through to OAuth, not
    return {"api_key": ""} which the SDK would reject."""
    env = _isolated_env(
        ANTHROPIC_API_KEY="",
        CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat1-USE-ME",
    )
    with patch.dict(os.environ, env, clear=True):
        assert resolve_anthropic_auth() == {"auth_token": "sk-ant-oat1-USE-ME"}
