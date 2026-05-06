"""Tests for the multi-CLI onboarding kit generator and rotate helper."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from onboarding_kit import VALID_CLIS, write_onboarding_kit


COMMON_KWARGS = dict(
    dev_id="alice",
    full_name="Alice Liddell",
    email="alice@example.com",
    invite_token="dvbn_inv_TESTTOKEN",
    callback_base="https://devbrain.example.com/onboard/dvbn_inv_TESTTOKEN",
    expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
    bootstrap_private_key="-----BEGIN OPENSSH PRIVATE KEY-----\nFAKEKEY\n-----END OPENSSH PRIVATE KEY-----\n",
    bootstrap_invite_id_short="abcd1234",
    bootstrap_expiry=datetime(2099, 1, 4, tzinfo=timezone.utc),
    ssh_host="lhts-mac-studio.local",
)


def _write(tmp_path: Path, cli: str = "claude") -> str:
    kit_path = tmp_path / f"alice-onboard-{cli}.md"
    write_onboarding_kit(path=kit_path, cli=cli, **COMMON_KWARGS)
    return kit_path.read_text()


# ─── Shared invariants ────────────────────────────────────────────────────────

def test_valid_clis_tuple():
    assert set(VALID_CLIS) == {"claude", "codex", "gemini"}


def test_invalid_cli_raises(tmp_path):
    with pytest.raises(ValueError, match="cli must be one of"):
        write_onboarding_kit(path=tmp_path / "kit.md", cli="gpt4", **COMMON_KWARGS)


def test_kit_file_mode_600(tmp_path):
    kit_path = tmp_path / "alice-onboard.md"
    write_onboarding_kit(path=kit_path, **COMMON_KWARGS)
    import stat
    mode = kit_path.stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_frontmatter_present_all_clis(tmp_path):
    for cli in VALID_CLIS:
        content = _write(tmp_path, cli)
        assert "devbrain_invite_token: dvbn_inv_TESTTOKEN" in content
        assert "dev_id: alice" in content
        assert f"cli: {cli}" in content


def test_phase1_ssh_keygen_present_all_clis(tmp_path):
    """Phase 1 (generate SSH keypair) must appear in every CLI's kit."""
    for cli in VALID_CLIS:
        content = _write(tmp_path, cli)
        assert "ssh-keygen -t ed25519" in content
        assert "id_ed25519_devbrain" in content


def test_phase4_bootstrap_key_block_present_all_clis(tmp_path):
    """Phase 4 (stage bootstrap key) must appear in every CLI's kit."""
    for cli in VALID_CLIS:
        content = _write(tmp_path, cli)
        assert "devbrain-bootstrap-alice" in content
        assert "BOOTSTRAP_KEY_END" in content


def test_phase8_verify_present_all_clis(tmp_path):
    """Phase 8 (verify SSH + factory dashboard) must appear in every CLI's kit."""
    for cli in VALID_CLIS:
        content = _write(tmp_path, cli)
        assert "lhtdev@lhts-mac-studio.local whoami" in content
        assert "factory dashboard" in content


def test_bootstrap_private_key_embedded(tmp_path):
    content = _write(tmp_path)
    assert "FAKEKEY" in content


# ─── Claude-specific ─────────────────────────────────────────────────────────

def test_claude_install_section(tmp_path):
    content = _write(tmp_path, "claude")
    assert "brew install --cask claude" in content
    assert "claude /login" in content


def test_claude_token_section(tmp_path):
    content = _write(tmp_path, "claude")
    assert "claude setup-token" in content
    assert "sk-ant-oat01-" in content


def test_claude_rotation_payload(tmp_path):
    content = _write(tmp_path, "claude")
    assert "oauth_token" in content
    # Should NOT mention codex_auth_json or gemini_api_key
    assert "codex_auth_json" not in content
    assert "gemini_api_key" not in content


def test_claude_error_messages(tmp_path):
    content = _write(tmp_path, "claude")
    assert "oauth_token_rejected_by_anthropic" in content


# ─── Codex-specific ───────────────────────────────────────────────────────────

def test_codex_install_section(tmp_path):
    content = _write(tmp_path, "codex")
    assert "npm install" in content
    assert "@openai/codex" in content


def test_codex_login_section(tmp_path):
    content = _write(tmp_path, "codex")
    assert "codex login --device-auth" in content
    assert "device-code" in content.lower() or "device code" in content.lower()


def test_codex_token_section(tmp_path):
    content = _write(tmp_path, "codex")
    assert "auth.json" in content
    assert ".codex" in content


def test_codex_rotation_payload(tmp_path):
    content = _write(tmp_path, "codex")
    assert "codex_auth_json" in content
    # Should NOT mention oauth_token or gemini_api_key
    assert "oauth_token" not in content
    assert "gemini_api_key" not in content


def test_codex_error_messages(tmp_path):
    content = _write(tmp_path, "codex")
    assert "codex_auth_json_invalid" in content


# ─── Gemini-specific ──────────────────────────────────────────────────────────

def test_gemini_install_section(tmp_path):
    content = _write(tmp_path, "gemini")
    assert "npm install" in content
    assert "@google/gemini-cli" in content


def test_gemini_token_section(tmp_path):
    content = _write(tmp_path, "gemini")
    assert "aistudio.google.com" in content
    assert "AIza" in content


def test_gemini_rotation_payload(tmp_path):
    content = _write(tmp_path, "gemini")
    assert "gemini_api_key" in content
    # Should NOT mention oauth_token or codex_auth_json
    assert "oauth_token" not in content
    assert "codex_auth_json" not in content


def test_gemini_error_messages(tmp_path):
    content = _write(tmp_path, "gemini")
    assert "gemini_api_key_rejected" in content


# ─── Backward compatibility ───────────────────────────────────────────────────

def test_default_cli_is_claude(tmp_path):
    """Calling write_onboarding_kit with no cli arg → claude kit."""
    kit_path = tmp_path / "alice-default.md"
    write_onboarding_kit(path=kit_path, **COMMON_KWARGS)
    content = kit_path.read_text()
    assert "brew install --cask claude" in content
    assert "claude setup-token" in content
    assert "oauth_token" in content


def test_mcp_config_path_varies_by_cli(tmp_path):
    """Each CLI's Phase 7 MCP config path must differ."""
    from onboarding_kit import _MCP_CONFIG_PATHS
    # Ensure each CLI has its own distinct config path
    assert len(set(_MCP_CONFIG_PATHS.values())) == len(VALID_CLIS), \
        f"Expected {len(VALID_CLIS)} distinct MCP config paths, got: {_MCP_CONFIG_PATHS}"

    # Ensure each path appears in the corresponding CLI's kit content
    for cli in VALID_CLIS:
        content = _write(tmp_path, cli)
        assert _MCP_CONFIG_PATHS[cli] in content, \
            f"MCP config path {_MCP_CONFIG_PATHS[cli]!r} not found in {cli} kit"


# ─── Rotate helper: accept each CLI's payload ─────────────────────────────────

def _run_helper(stdin_body: dict, invite_id_short: str = "abcd1234") -> int:
    """Run onboard_rotate_helper.main() with mocked DB and stdin.

    Returns the process exit code. The helper does lazy imports of FactoryDB
    and DATABASE_URL inside main(), so we patch them at their source modules.
    """
    import io
    import onboard_rotate_helper as _h

    fake_row = ("some-uuid-here", "alice", "ready")

    mock_cur = MagicMock()
    mock_cur.fetchone.return_value = fake_row
    mock_cur.__enter__ = lambda s: s
    mock_cur.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__ = MagicMock(return_value=False)

    mock_db = MagicMock()
    mock_db._conn.return_value = mock_conn

    mock_factory_db_cls = MagicMock(return_value=mock_db)

    original_argv = sys.argv
    try:
        sys.argv = ["onboard_rotate_helper.py", "--invite-id-short", invite_id_short]
        original_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(stdin_body))

        # Patch at the state_machine and config module level (where helper
        # imports them lazily inside main()).
        with patch("state_machine.FactoryDB", mock_factory_db_cls), \
             patch("config.DATABASE_URL", "postgresql://fake/fake"):
            return _h.main()
    finally:
        sys.argv = original_argv
        sys.stdin = original_stdin


def test_rotate_helper_accepts_claude_payload():
    body = {
        "pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI alice@example.com",
        "cli": "claude",
        "oauth_token": "sk-ant-oat01-FAKETOKEN",
    }
    assert _run_helper(body) == 0


def test_rotate_helper_accepts_codex_payload():
    body = {
        "pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI alice@example.com",
        "cli": "codex",
        "codex_auth_json": {"accessToken": "sk-ant-oat01-FAKECODEXTOKEN", "other": "data"},
    }
    assert _run_helper(body) == 0


def test_rotate_helper_accepts_gemini_payload():
    body = {
        "pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI alice@example.com",
        "cli": "gemini",
        "gemini_api_key": "AIzaSyFAKEKEY",
    }
    assert _run_helper(body) == 0


def test_rotate_helper_rejects_bad_pubkey():
    body = {
        "pubkey": "not-a-valid-key",
        "cli": "claude",
        "oauth_token": "sk-ant-oat01-FAKE",
    }
    assert _run_helper(body) == 2


def test_rotate_helper_rejects_unknown_cli():
    body = {
        "pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI alice@example.com",
        "cli": "gpt5",
        "oauth_token": "sk-ant-oat01-FAKE",
    }
    assert _run_helper(body) == 2


def test_rotate_helper_missing_gemini_key_returns_error():
    body = {
        "pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI alice@example.com",
        "cli": "gemini",
        # gemini_api_key intentionally omitted
    }
    assert _run_helper(body) == 2


def test_rotate_helper_gemini_format_check():
    """Gemini API keys must start with AIza."""
    body = {
        "pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI alice@example.com",
        "cli": "gemini",
        "gemini_api_key": "not-a-google-key",
    }
    assert _run_helper(body) == 2
