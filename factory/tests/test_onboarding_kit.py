"""Tests for the redesigned onboarding kit generator and rotate helper."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from onboarding_kit import (
    VALID_AGENT_APPS,
    VALID_CLIS,
    VALID_PLATFORMS,
    write_onboarding_kit,
)


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
    ssh_host_fingerprint="SHA256:test+fingerprint+for+unit+tests=",
)


def _write(tmp_path: Path, cli: str = "claude", **overrides) -> str:
    kwargs = {**COMMON_KWARGS, **overrides}
    kit_path = tmp_path / f"alice-onboard-{cli}.md"
    write_onboarding_kit(path=kit_path, cli=cli, **kwargs)
    return kit_path.read_text()


# ─── Validation + invariants ─────────────────────────────────────────────────

def test_valid_clis_tuple():
    assert set(VALID_CLIS) == {"claude", "codex", "gemini"}


def test_valid_platforms_tuple():
    assert set(VALID_PLATFORMS) == {"auto", "mac", "linux", "windows"}


def test_valid_agent_apps_tuple():
    assert set(VALID_AGENT_APPS) == {
        "auto",
        "claude-desktop", "codex-desktop", "gemini-desktop",
        "claude-cli", "codex-cli", "gemini-cli",
    }


def test_invalid_cli_raises(tmp_path):
    with pytest.raises(ValueError, match="cli must be one of"):
        write_onboarding_kit(path=tmp_path / "kit.md", cli="gpt4", **COMMON_KWARGS)


def test_invalid_platform_raises(tmp_path):
    with pytest.raises(ValueError, match="platform must be one of"):
        write_onboarding_kit(
            path=tmp_path / "kit.md", platform="bsd", **COMMON_KWARGS
        )


def test_invalid_agent_app_raises(tmp_path):
    with pytest.raises(ValueError, match="agent_app must be one of"):
        write_onboarding_kit(
            path=tmp_path / "kit.md", agent_app="cursor", **COMMON_KWARGS
        )


def test_kit_file_mode_600(tmp_path):
    kit_path = tmp_path / "alice-onboard.md"
    write_onboarding_kit(path=kit_path, **COMMON_KWARGS)
    import stat
    mode = kit_path.stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


# ─── Frontmatter ─────────────────────────────────────────────────────────────

def test_frontmatter_includes_axes(tmp_path):
    """All three issuance axes (cli, platform, agent_app) appear in frontmatter."""
    content = _write(tmp_path, cli="claude", platform="mac", agent_app="claude-desktop")
    assert "cli: claude" in content
    assert "platform: mac" in content
    assert "agent_app: claude-desktop" in content


def test_frontmatter_includes_invitation_id(tmp_path):
    content = _write(tmp_path)
    assert "devbrain_invite_id_short: abcd1234" in content


def test_frontmatter_includes_host_fingerprint(tmp_path):
    content = _write(tmp_path)
    assert "SHA256:test+fingerprint+for+unit+tests=" in content


def test_frontmatter_present_all_clis(tmp_path):
    for cli in VALID_CLIS:
        content = _write(tmp_path, cli)
        assert "devbrain_invite_token: dvbn_inv_TESTTOKEN" in content
        assert "dev_id: alice" in content
        assert f"cli: {cli}" in content


# ─── Phase 0: Trust banner ───────────────────────────────────────────────────

def test_phase0_trust_banner_present_all_clis(tmp_path):
    """Every kit leads with the trust banner."""
    for cli in VALID_CLIS:
        content = _write(tmp_path, cli)
        assert "## Phase 0 — Verification" in content
        assert "Lighthouse Therapy / DevBrain" in content
        assert "SSH host key fingerprint" in content


def test_phase0_includes_invitation_id_for_user_verification(tmp_path):
    content = _write(tmp_path)
    # The user/agent verifies the invitation ID matches what the admin said
    assert "abcd1234" in content


def test_phase0_includes_host_fingerprint_for_first_connect_verification(tmp_path):
    content = _write(tmp_path)
    assert "SHA256:test+fingerprint+for+unit+tests=" in content


def test_phase0_falls_back_when_fingerprint_unset(tmp_path):
    content = _write(tmp_path, ssh_host_fingerprint="")
    assert "(verify on first SSH connect)" in content


def test_phase0_asks_user_to_confirm_intent(tmp_path):
    content = _write(tmp_path)
    assert "agent:human" in content
    assert "intent-confirmation" in content


# ─── Phase 1: Environment check + dep install ────────────────────────────────

def test_phase1_bash_present_when_platform_mac_or_linux(tmp_path):
    for platform in ("mac", "linux"):
        content = _write(tmp_path, platform=platform)
        assert "macOS / Linux" in content
        assert "command -v ssh" in content or "command -v \"$cmd\"" in content
        # PowerShell variant should NOT appear when platform-specific
        assert "Get-WindowsCapability" not in content


def test_phase1_powershell_present_when_platform_windows(tmp_path):
    content = _write(tmp_path, platform="windows")
    assert "Get-WindowsCapability -Online -Name 'OpenSSH.Client*'" in content
    assert "Add-WindowsCapability" in content
    # Bash variant should NOT appear
    assert "command -v" not in content


def test_phase1_includes_both_when_platform_auto(tmp_path):
    content = _write(tmp_path, platform="auto")
    # Both shell variants present
    assert "macOS / Linux" in content
    assert "Get-WindowsCapability" in content


# ─── Phase 2: Generate SSH keypair ───────────────────────────────────────────

def test_phase2_ssh_keygen_present_all_clis(tmp_path):
    """Every kit teaches the dev to generate a permanent ssh keypair."""
    for cli in VALID_CLIS:
        content = _write(tmp_path, cli)
        assert "ssh-keygen -t ed25519" in content
        assert "id_ed25519_devbrain" in content


def test_phase2_powershell_uses_userprofile_path(tmp_path):
    """Windows variant uses %USERPROFILE% (PowerShell idiom), not ~/."""
    content = _write(tmp_path, platform="windows")
    assert "$env:USERPROFILE" in content


# ─── Phase 3: Bootstrap key staging ──────────────────────────────────────────

def test_phase3_bootstrap_key_block_present_all_clis(tmp_path):
    for cli in VALID_CLIS:
        content = _write(tmp_path, cli)
        assert "devbrain-bootstrap-alice" in content


def test_phase3_bash_uses_chmod_600(tmp_path):
    content = _write(tmp_path, platform="mac")
    assert "chmod 600" in content


def test_phase3_powershell_uses_icacls(tmp_path):
    """Windows ssh.exe rejects keys with loose ACLs — kit must lock them down."""
    content = _write(tmp_path, platform="windows")
    assert "icacls" in content
    assert "/inheritance:r" in content


def test_bootstrap_private_key_embedded(tmp_path):
    content = _write(tmp_path)
    assert "FAKEKEY" in content


# ─── Phase 4: Rotate pubkey to server ────────────────────────────────────────

def test_phase4_payload_is_pubkey_only_all_clis(tmp_path):
    """The rotate payload no longer carries credentials — pubkey only.

    Targets the JSON shape inside the Phase 4 code block specifically.
    Other parts of the kit can mention credential field names (e.g.,
    `secret=gemini_api_key` agent metadata in Phase 5), but the rotate
    request body itself must be pubkey-only.
    """
    for cli in VALID_CLIS:
        content = _write(tmp_path, cli)
        # Slice out Phase 4's content
        p4_start = content.index("## Phase 4")
        p5_start = content.index("## Phase 5")
        p4 = content[p4_start:p5_start]
        # Phase 4 carries only the pubkey field in its rotate payload
        assert '"pubkey"' in p4
        assert "oauth_token" not in p4
        assert "codex_auth_json" not in p4
        assert "gemini_api_key" not in p4


def test_phase4_bash_uses_pipe_form(tmp_path):
    """Phase 4's bash variant uses a regular pipe, not process substitution."""
    content = _write(tmp_path, platform="mac")
    assert "| ssh" in content
    assert "< <(" not in content


def test_phase4_powershell_uses_native_json(tmp_path):
    content = _write(tmp_path, platform="windows")
    assert "ConvertTo-Json" in content


# ─── Phase 5: Server-side devbrain login ─────────────────────────────────────

def test_phase5_invokes_devbrain_login(tmp_path):
    for cli in VALID_CLIS:
        content = _write(tmp_path, cli)
        assert f"devbrain login --dev alice --cli {cli}" in content


def test_phase5_uses_t_flag_for_interactive_session(tmp_path):
    content = _write(tmp_path)
    # -t flag is required for the paste-code-back interactive flow
    assert " -t " in content or " -t \\\n" in content or "-t lhtdev@" in content


def test_phase5_claude_mentions_setup_token_server_side(tmp_path):
    content = _write(tmp_path, cli="claude")
    assert "claude setup-token" in content
    assert "server-side" in content


def test_phase5_codex_mentions_device_auth(tmp_path):
    content = _write(tmp_path, cli="codex")
    assert "codex login --device-auth" in content


def test_phase5_gemini_prompts_for_api_key(tmp_path):
    content = _write(tmp_path, cli="gemini")
    assert "aistudio.google.com" in content
    assert "AIza" in content


def test_phase5_emphasizes_token_never_returns_to_dev(tmp_path):
    """The whole point of the redesign — make this explicit in every kit.

    The kit's preamble + Phase 0 + Phase 5 together must clearly state
    that the auth credential never transits the dev's machine. We just
    grep for the canonical phrase from Phase 0's invariants list.
    """
    for cli in VALID_CLIS:
        content = _write(tmp_path, cli)
        assert "never leaves the Mac Studio" in content


# ─── Phase 6: MCP wire-up + verify ───────────────────────────────────────────

def test_phase6_mcp_config_path_varies_by_cli(tmp_path):
    from onboarding_kit import _MCP_CONFIG_PATHS
    assert len(set(_MCP_CONFIG_PATHS.values())) == len(VALID_CLIS)
    for cli in VALID_CLIS:
        content = _write(tmp_path, cli)
        assert _MCP_CONFIG_PATHS[cli] in content, \
            f"MCP config path {_MCP_CONFIG_PATHS[cli]!r} not found in {cli} kit"


def test_phase6_verify_command_present_all_clis(tmp_path):
    for cli in VALID_CLIS:
        content = _write(tmp_path, cli)
        assert "lhtdev@lhts-mac-studio.local" in content
        assert "whoami" in content


# ─── Phase 7: Cleanup ────────────────────────────────────────────────────────

def test_phase7_removes_bootstrap_key_all_clis(tmp_path):
    for cli in VALID_CLIS:
        content = _write(tmp_path, cli)
        # bash variant uses shred or rm
        assert "rm -f ~/.ssh/devbrain-bootstrap-alice" in content \
            or "shred -u ~/.ssh/devbrain-bootstrap-alice" in content
        # PowerShell variant uses Remove-Item (when auto or windows)


def test_phase7_powershell_uses_remove_item(tmp_path):
    content = _write(tmp_path, platform="windows")
    assert "Remove-Item" in content


# ─── Agent-app axis ──────────────────────────────────────────────────────────

def test_agent_app_appears_in_phase6_text(tmp_path):
    """When the dev's agent app is specified, it's named in Phase 6 framing."""
    content = _write(tmp_path, agent_app="claude-desktop")
    assert "Claude Desktop" in content


def test_agent_app_auto_uses_generic_phrasing(tmp_path):
    content = _write(tmp_path, agent_app="auto")
    assert "your AI agent" in content


# ─── Default values ──────────────────────────────────────────────────────────

def test_default_platform_is_auto(tmp_path):
    """Omitting platform defaults to 'auto'."""
    kit_default = tmp_path / "default.md"
    kit_auto = tmp_path / "auto.md"
    write_onboarding_kit(path=kit_default, **COMMON_KWARGS)
    write_onboarding_kit(path=kit_auto, platform="auto", **COMMON_KWARGS)
    assert kit_default.read_text() == kit_auto.read_text()


def test_default_agent_app_is_auto(tmp_path):
    kit_default = tmp_path / "default.md"
    kit_auto = tmp_path / "auto.md"
    write_onboarding_kit(path=kit_default, **COMMON_KWARGS)
    write_onboarding_kit(path=kit_auto, agent_app="auto", **COMMON_KWARGS)
    assert kit_default.read_text() == kit_auto.read_text()


# ─── Rotate helper: pubkey-only payloads ─────────────────────────────────────

def _run_helper(stdin_body: dict, invite_id_short: str = "abcd1234") -> int:
    """Run onboard_rotate_helper.main() with mocked DB and stdin."""
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

        with patch("state_machine.FactoryDB", mock_factory_db_cls), \
             patch("config.DATABASE_URL", "postgresql://fake/fake"):
            return _h.main()
    finally:
        sys.argv = original_argv
        sys.stdin = original_stdin


def test_rotate_helper_accepts_claude_pubkey_only():
    body = {
        "pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI alice@example.com",
        "cli": "claude",
    }
    assert _run_helper(body) == 0


def test_rotate_helper_accepts_codex_pubkey_only():
    body = {
        "pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI alice@example.com",
        "cli": "codex",
    }
    assert _run_helper(body) == 0


def test_rotate_helper_accepts_gemini_pubkey_only():
    body = {
        "pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI alice@example.com",
        "cli": "gemini",
    }
    assert _run_helper(body) == 0


def test_rotate_helper_ignores_legacy_credential_fields():
    """Older kits (pre-2026-05-07) shipped credentials in the rotate payload.
    The new helper ignores those fields rather than rejecting."""
    body = {
        "pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI alice@example.com",
        "cli": "claude",
        "oauth_token": "sk-ant-oat01-LEGACY-IGNORED",
    }
    assert _run_helper(body) == 0


def test_rotate_helper_rejects_bad_pubkey():
    body = {
        "pubkey": "not-a-valid-key",
        "cli": "claude",
    }
    assert _run_helper(body) == 2


def test_rotate_helper_rejects_unknown_cli():
    body = {
        "pubkey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI alice@example.com",
        "cli": "gpt5",
    }
    assert _run_helper(body) == 2


def test_rotate_helper_rejects_missing_pubkey():
    body = {"cli": "claude"}
    assert _run_helper(body) == 2
