"""Tests for cognify.setup_launchd — plist renderer + installer."""
from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from cognify.setup_launchd import (
    ALL_PLISTS,
    CredentialChoice,
    _CRED_PLISTS,
    _NO_CRED_PLISTS,
    _TEMPLATE_DIR,
    install_cognify_launchd,
    render_plist,
    resolve_credential_from_env,
)


# ─── CredentialChoice ────────────────────────────────────────────────────────


def test_credential_choice_rejects_unknown_env_name():
    with pytest.raises(ValueError, match="unsupported credential env_name"):
        CredentialChoice("X_API_KEY", "value")


def test_credential_choice_rejects_empty_value():
    with pytest.raises(ValueError, match="must be non-empty"):
        CredentialChoice("ANTHROPIC_API_KEY", "")


def test_credential_choice_accepts_api_key():
    c = CredentialChoice("ANTHROPIC_API_KEY", "sk-ant-api03-FAKE")
    assert c.env_name == "ANTHROPIC_API_KEY"
    assert c.value == "sk-ant-api03-FAKE"


def test_credential_choice_accepts_oauth_token():
    c = CredentialChoice("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-FAKE")
    assert c.env_name == "CLAUDE_CODE_OAUTH_TOKEN"


# ─── resolve_credential_from_env ─────────────────────────────────────────────


def _isolated_env(**vars):
    base = {k: v for k, v in os.environ.items()
            if k not in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")}
    base.update(vars)
    return base


def test_resolve_returns_none_when_no_env():
    with patch.dict(os.environ, _isolated_env(), clear=True):
        assert resolve_credential_from_env() is None


def test_resolve_picks_console_key():
    with patch.dict(os.environ, _isolated_env(ANTHROPIC_API_KEY="sk-ant-api03-K"), clear=True):
        c = resolve_credential_from_env()
        assert c == CredentialChoice("ANTHROPIC_API_KEY", "sk-ant-api03-K")


def test_resolve_picks_oauth_when_only_oauth_set():
    with patch.dict(os.environ, _isolated_env(CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-T"), clear=True):
        c = resolve_credential_from_env()
        assert c == CredentialChoice("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-T")


def test_resolve_console_wins_over_oauth():
    with patch.dict(
        os.environ,
        _isolated_env(
            ANTHROPIC_API_KEY="sk-ant-api03-WINS",
            CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-LOSES",
        ),
        clear=True,
    ):
        c = resolve_credential_from_env()
        assert c.env_name == "ANTHROPIC_API_KEY"


# ─── render_plist ────────────────────────────────────────────────────────────


def _read_template(name: str) -> str:
    return (_TEMPLATE_DIR / name).read_text()


def test_render_substitutes_devbrain_home_and_user():
    text = _read_template("com.devbrain.cognify-decay.plist")
    out = render_plist(
        text,
        devbrain_home="/tmp/devbrain",
        user="alice",
        project_slug="ignored-for-decay",  # decay doesn't use slug
        credential=None,
    )
    assert "${DEVBRAIN_HOME}" not in out
    assert "${USER}" not in out
    assert "/tmp/devbrain/.venv/bin/python" in out
    assert "/Users/alice/.devbrain/logs/cognify-decay.log" in out


def test_render_substitutes_project_slug():
    text = _read_template("com.devbrain.cognify-strengthen.plist")
    out = render_plist(
        text,
        devbrain_home="/tmp/devbrain",
        user="alice",
        project_slug="brightbot",
        credential=None,
    )
    assert "${PROJECT_SLUG}" not in out
    assert "--project=brightbot" in out


def test_render_inserts_oauth_credential_block_for_extract():
    text = _read_template("com.devbrain.cognify-extract.plist")
    cred = CredentialChoice("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-FAKE")
    out = render_plist(
        text,
        devbrain_home="/tmp/devbrain",
        user="alice",
        project_slug="brightbot",
        credential=cred,
    )
    # The credential block is now present...
    assert "<key>CLAUDE_CODE_OAUTH_TOKEN</key>" in out
    assert "<string>sk-ant-oat01-FAKE</string>" in out
    # ...and the marker is gone.
    assert "@CREDENTIAL_ENV_BLOCK@" not in out
    # ...and ANTHROPIC_API_KEY is NOT emitted (since we used OAuth).
    assert "<key>ANTHROPIC_API_KEY</key>" not in out


def test_render_inserts_api_key_credential_block_for_edges():
    text = _read_template("com.devbrain.cognify-edges.plist")
    cred = CredentialChoice("ANTHROPIC_API_KEY", "sk-ant-api03-CONSOLE")
    out = render_plist(
        text,
        devbrain_home="/tmp/devbrain",
        user="alice",
        project_slug="brightbot",
        credential=cred,
    )
    assert "<key>ANTHROPIC_API_KEY</key>" in out
    assert "<string>sk-ant-api03-CONSOLE</string>" in out
    assert "<key>CLAUDE_CODE_OAUTH_TOKEN</key>" not in out
    assert "@CREDENTIAL_ENV_BLOCK@" not in out


def test_render_raises_when_extract_template_has_no_credential():
    text = _read_template("com.devbrain.cognify-extract.plist")
    with pytest.raises(ValueError, match="require one of"):
        render_plist(
            text,
            devbrain_home="/tmp/devbrain",
            user="alice",
            project_slug="brightbot",
            credential=None,
        )


def test_render_no_credential_block_in_non_llm_templates():
    """decay, strengthen, gc don't have the marker — credential=None must be OK."""
    for name in _NO_CRED_PLISTS:
        text = _read_template(name)
        out = render_plist(
            text,
            devbrain_home="/tmp/devbrain",
            user="alice",
            project_slug="brightbot",
            credential=None,
        )
        # No traces of credentials
        assert "ANTHROPIC_API_KEY" not in out
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in out
        # No literal placeholders left
        assert "${DEVBRAIN_HOME}" not in out
        assert "${USER}" not in out
        # Indentation preserved
        assert "<plist version=\"1.0\">" in out


def test_render_does_not_carry_placeholder_for_project_in_no_project_plists():
    """decay + gc don't take --project, so ${PROJECT_SLUG} shouldn't appear
    in their templates at all (sanity check the templates themselves)."""
    for name in ("com.devbrain.cognify-decay.plist", "com.devbrain.cognify-gc.plist"):
        text = _read_template(name)
        assert "${PROJECT_SLUG}" not in text, (
            f"{name} should not have ${{PROJECT_SLUG}} — it doesn't pass --project"
        )


# ─── install_cognify_launchd ─────────────────────────────────────────────────


def test_install_writes_all_plists(tmp_path):
    cred = CredentialChoice("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-FAKE")
    installed = install_cognify_launchd(
        project_slug="brightbot",
        credential=cred,
        devbrain_home="/tmp/devbrain",
        user="alice",
        target_dir=tmp_path,
        reload=False,
    )
    # 6 plists post-Phase-8: 3 SQL-only (decay/strengthen/gc) +
    # 3 LLM-cost (extract/edges/fanout).
    assert len(installed) == len(ALL_PLISTS)
    assert {p.name for p in installed} == set(ALL_PLISTS)
    for p in installed:
        assert p.exists()
        assert p.parent == tmp_path


def test_install_sets_chmod_0600_on_credential_plists(tmp_path):
    cred = CredentialChoice("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-FAKE")
    installed = install_cognify_launchd(
        project_slug="brightbot",
        credential=cred,
        devbrain_home="/tmp/devbrain",
        user="alice",
        target_dir=tmp_path,
        reload=False,
    )
    for p in installed:
        mode = stat.S_IMODE(p.stat().st_mode)
        if p.name in _CRED_PLISTS:
            assert mode == 0o600, f"{p.name} should be 0600, got {oct(mode)}"
        else:
            assert mode == 0o644, f"{p.name} should be 0644, got {oct(mode)}"


def test_install_is_idempotent(tmp_path):
    cred = CredentialChoice("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-FAKE")
    first = install_cognify_launchd(
        project_slug="brightbot",
        credential=cred,
        devbrain_home="/tmp/devbrain",
        user="alice",
        target_dir=tmp_path,
        reload=False,
    )
    second = install_cognify_launchd(
        project_slug="brightbot",
        credential=cred,
        devbrain_home="/tmp/devbrain",
        user="alice",
        target_dir=tmp_path,
        reload=False,
    )
    assert [p.name for p in first] == [p.name for p in second]
    # No leftover .tmp files
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


def test_install_with_reload_invokes_launchctl(tmp_path):
    calls: list[tuple[str, ...]] = []

    def fake_run(args, **kw):
        calls.append(tuple(args))
        class Result:
            returncode = 0
            stdout = b""
            stderr = b""
        return Result()

    cred = CredentialChoice("ANTHROPIC_API_KEY", "sk-ant-api03-FAKE")
    install_cognify_launchd(
        project_slug="brightbot",
        credential=cred,
        devbrain_home="/tmp/devbrain",
        user="alice",
        target_dir=tmp_path,
        reload=True,
        runner=fake_run,
    )
    # Two calls per plist (unload + load) × len(ALL_PLISTS).
    assert len(calls) == 2 * len(ALL_PLISTS)
    # Each pair should be (launchctl unload <path>) then (launchctl load <path>)
    for unload_call, load_call in zip(calls[0::2], calls[1::2]):
        assert unload_call[0:2] == ("launchctl", "unload")
        assert load_call[0:2] == ("launchctl", "load")
        assert unload_call[2] == load_call[2]  # same plist


def test_install_resolves_credential_from_env_if_not_passed(tmp_path):
    with patch.dict(
        os.environ,
        _isolated_env(CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-FROM-ENV"),
        clear=True,
    ):
        installed = install_cognify_launchd(
            project_slug="brightbot",
            devbrain_home="/tmp/devbrain",
            user="alice",
            target_dir=tmp_path,
            reload=False,
        )
    # Inspect the extract plist to confirm the env-resolved cred was used.
    extract = next(p for p in installed if p.name == "com.devbrain.cognify-extract.plist")
    body = extract.read_text()
    assert "<key>CLAUDE_CODE_OAUTH_TOKEN</key>" in body
    assert "sk-ant-oat01-FROM-ENV" in body


def test_install_errors_if_no_cred_in_env_and_not_passed(tmp_path):
    with patch.dict(os.environ, _isolated_env(), clear=True):
        with pytest.raises(ValueError, match="require one of"):
            install_cognify_launchd(
                project_slug="brightbot",
                devbrain_home="/tmp/devbrain",
                user="alice",
                target_dir=tmp_path,
                reload=False,
            )
