"""Tests for the onboarding email sender.

Verifies that:
  - email body is short (not the full kit)
  - the .md kit is delivered as an attachment
  - attachment bytes equal kit_path.read_bytes()
  - the canonical filename (<dev_id>-onboarding-kit.md) is used
  - the CLI display name appears in the body
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from onboarding_email import send_onboarding_email


KIT_CONTENT = """\
---
devbrain_invite_token: dvbn_inv_TESTTOKEN
dev_id: alice
cli: claude
---

# Welcome to BrightBot, Alice 👋

This is a test kit with enough content to be realistic but not overly long.

## Phase 1 — Generate your permanent SSH keypair

ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_devbrain -C "alice@example.com" -N ""

## Phase 2 — Install Claude Code

brew install --cask claude
claude /login

## Phase 3 — Generate token

claude setup-token

(and many more phases...)
"""


@pytest.fixture
def kit_file(tmp_path: Path) -> Path:
    p = tmp_path / "alice-onboard.md"
    p.write_text(KIT_CONTENT)
    p.chmod(0o600)
    return p


def _make_channel(delivered: bool = True, error: str | None = None):
    """Return a mock channel whose send() captures kwargs."""
    from notifications.base import ChannelResult
    result = ChannelResult(delivered=delivered, channel="smtp", error=error)
    ch = MagicMock()
    ch.name = "smtp"
    ch.is_configured.return_value = True
    ch.send.return_value = result
    return ch


def _send(kit_file: Path, cli: str = "claude", channel=None) -> tuple[bool, Any]:
    """Call send_onboarding_email and return (success, channel_mock)."""
    if channel is None:
        channel = _make_channel()
    with patch("onboarding_email._pick_email_channel", return_value=channel):
        success = send_onboarding_email(
            to_email="alice@example.com",
            dev_id="alice",
            full_name="Alice Liddell",
            kit_path=kit_file,
            admin_name="patrick",
            admin_contact="patrick",
            cli=cli,
        )
    return success, channel


# ─── Core attachment tests ────────────────────────────────────────────────────

def test_send_returns_true_on_success(kit_file):
    success, _ = _send(kit_file)
    assert success is True


def test_attachment_present(kit_file):
    """channel.send() must be called with a non-empty attachments list."""
    success, ch = _send(kit_file)
    call_kwargs = ch.send.call_args.kwargs
    assert "attachments" in call_kwargs
    assert len(call_kwargs["attachments"]) == 1


def test_attachment_bytes_equal_kit_bytes(kit_file):
    """The attachment file bytes must equal kit_path.read_bytes()."""
    success, ch = _send(kit_file)
    att_path: Path = ch.send.call_args.kwargs["attachments"][0]
    assert att_path.read_bytes() == kit_file.read_bytes()


def test_attachment_filename_is_canonical(kit_file):
    """Attachment must be named <dev_id>-onboarding-kit.md."""
    success, ch = _send(kit_file)
    att_path: Path = ch.send.call_args.kwargs["attachments"][0]
    assert att_path.name == "alice-onboarding-kit.md"


def test_body_does_not_contain_full_kit(kit_file):
    """Email body must NOT contain the full kit Markdown content."""
    success, ch = _send(kit_file)
    body: str = ch.send.call_args.kwargs["body"]
    # Body must not contain multi-line kit content markers
    assert "ssh-keygen" not in body
    assert "brew install --cask" not in body
    assert "BOOTSTRAP_KEY_END" not in body


def test_body_mentions_attachment_filename(kit_file):
    """Body must reference the attachment filename so the dev knows what to open."""
    success, ch = _send(kit_file)
    body: str = ch.send.call_args.kwargs["body"]
    assert "alice-onboarding-kit.md" in body


def test_body_is_short(kit_file):
    """Body must be under 2000 chars — the full kit runs 5-15KB."""
    success, ch = _send(kit_file)
    body: str = ch.send.call_args.kwargs["body"]
    assert len(body) < 2000, (
        f"Email body is {len(body)} chars — expected a short welcome message, "
        "not a full kit dump"
    )


# ─── CLI personalisation ──────────────────────────────────────────────────────

def test_body_contains_claude_display_name(kit_file):
    success, ch = _send(kit_file, cli="claude")
    body: str = ch.send.call_args.kwargs["body"]
    assert "Claude Code" in body


def test_body_contains_codex_display_name(kit_file):
    success, ch = _send(kit_file, cli="codex")
    body: str = ch.send.call_args.kwargs["body"]
    assert "Codex" in body


def test_body_contains_gemini_display_name(kit_file):
    success, ch = _send(kit_file, cli="gemini")
    body: str = ch.send.call_args.kwargs["body"]
    assert "Gemini CLI" in body


def test_subject_contains_cli_display_name(kit_file):
    success, ch = _send(kit_file, cli="codex")
    title: str = ch.send.call_args.kwargs["title"]
    assert "Codex" in title


# ─── Failure handling ─────────────────────────────────────────────────────────

def test_send_returns_false_on_channel_failure(kit_file):
    ch = _make_channel(delivered=False, error="SMTP timed out")
    success, _ = _send(kit_file, channel=ch)
    assert success is False


def test_send_returns_false_when_no_channel(kit_file):
    with patch("onboarding_email._pick_email_channel", return_value=None):
        result = send_onboarding_email(
            to_email="alice@example.com",
            dev_id="alice",
            full_name="Alice Liddell",
            kit_path=kit_file,
        )
    assert result is False


# ─── Canonical filename copy ──────────────────────────────────────────────────

def test_original_kit_file_unchanged_after_send(kit_file):
    """The original kit_path file must not be altered by the send operation."""
    original_content = kit_file.read_bytes()
    _send(kit_file)
    assert kit_file.read_bytes() == original_content


def test_no_copy_made_when_name_already_canonical(tmp_path):
    """When kit_path.name IS already <dev_id>-onboarding-kit.md, no copy is made."""
    canonical = tmp_path / "alice-onboarding-kit.md"
    canonical.write_text(KIT_CONTENT)
    ch = _make_channel()
    with patch("onboarding_email._pick_email_channel", return_value=ch):
        send_onboarding_email(
            to_email="alice@example.com",
            dev_id="alice",
            full_name="Alice Liddell",
            kit_path=canonical,
        )
    att_path: Path = ch.send.call_args.kwargs["attachments"][0]
    assert att_path == canonical
