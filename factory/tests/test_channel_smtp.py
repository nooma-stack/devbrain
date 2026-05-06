"""Tests for SmtpChannel."""
import email
import os
import smtplib
from email import message_from_bytes
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from notifications.channels.smtp import SmtpChannel


def _make_channel(**overrides):
    defaults = dict(
        host="smtp.example.com",
        port=587,
        use_tls=True,
        username="user@example.com",
        password="secret",
        sender_email="devbrain@example.com",
        sender_display_name="DevBrain",
    )
    defaults.update(overrides)
    return SmtpChannel(**defaults)


def test_is_configured_complete():
    ch = _make_channel()
    assert ch.is_configured() is True


def test_is_configured_missing_host():
    ch = _make_channel(host="")
    assert ch.is_configured() is False


def test_is_configured_missing_sender():
    ch = _make_channel(sender_email="")
    assert ch.is_configured() is False


def test_send_success():
    ch = _make_channel()
    mock_server = MagicMock()
    with patch("notifications.channels.smtp.smtplib.SMTP") as mock_smtp:
        mock_smtp.return_value.__enter__.return_value = mock_server
        result = ch.send("to@example.com", "Hello", "Body text")

    assert result.delivered is True
    assert result.channel == "smtp"
    mock_smtp.assert_called_once_with("smtp.example.com", 587, timeout=30)
    mock_server.starttls.assert_called_once()
    mock_server.login.assert_called_once_with("user@example.com", "secret")
    mock_server.send_message.assert_called_once()


def test_send_no_tls():
    ch = _make_channel(use_tls=False)
    mock_server = MagicMock()
    with patch("notifications.channels.smtp.smtplib.SMTP") as mock_smtp:
        mock_smtp.return_value.__enter__.return_value = mock_server
        result = ch.send("to@example.com", "Hello", "Body")

    assert result.delivered is True
    mock_server.starttls.assert_not_called()
    mock_server.send_message.assert_called_once()


def test_send_auth_failure():
    ch = _make_channel()
    mock_server = MagicMock()
    mock_server.login.side_effect = smtplib.SMTPAuthenticationError(
        535, b"Authentication failed"
    )
    with patch("notifications.channels.smtp.smtplib.SMTP") as mock_smtp:
        mock_smtp.return_value.__enter__.return_value = mock_server
        result = ch.send("to@example.com", "Hello", "Body")

    assert result.delivered is False
    assert result.error is not None
    assert "auth" in result.error.lower()


def test_env_var_credentials():
    ch = _make_channel(username="", password="")
    mock_server = MagicMock()
    env = {"SMTP_USERNAME": "env-user@example.com", "SMTP_PASSWORD": "env-secret"}
    with patch.dict(os.environ, env, clear=False), patch(
        "notifications.channels.smtp.smtplib.SMTP"
    ) as mock_smtp:
        mock_smtp.return_value.__enter__.return_value = mock_server
        result = ch.send("to@example.com", "Hello", "Body")

    assert result.delivered is True
    mock_server.login.assert_called_once_with("env-user@example.com", "env-secret")


# ─── Attachment tests ──────────────────────────────────────────────────────────

def _send_with_attachment(ch, tmp_path: Path, content: bytes, filename: str):
    """Helper: send with one attachment and return the captured MIMEMultipart msg."""
    att_file = tmp_path / filename
    att_file.write_bytes(content)

    captured_msgs: list = []
    mock_server = MagicMock()
    mock_server.send_message.side_effect = lambda m: captured_msgs.append(m)

    with patch("notifications.channels.smtp.smtplib.SMTP") as mock_smtp:
        mock_smtp.return_value.__enter__.return_value = mock_server
        result = ch.send(
            "to@example.com",
            "Attachment test",
            "Body",
            attachments=[att_file],
        )

    assert result.delivered is True
    assert len(captured_msgs) == 1
    return captured_msgs[0]


def test_send_with_attachment_bytes_preserved(tmp_path):
    """Attachment bytes must survive the MIME encode/decode round-trip."""
    ch = _make_channel()
    payload = b"\x00\x01\x02\x03SMTP_KIT_DATA\xff\xfe"
    msg = _send_with_attachment(ch, tmp_path, payload, "kit.md")

    # Serialize and re-parse to simulate wire transit
    raw = msg.as_bytes()
    parsed = message_from_bytes(raw)

    attachment_payload: bytes | None = None
    for part in parsed.walk():
        disp = part.get("Content-Disposition", "")
        if "attachment" in disp:
            attachment_payload = part.get_payload(decode=True)
            assert part.get_filename() == "kit.md"
            break

    assert attachment_payload is not None, "No attachment part found"
    assert attachment_payload == payload


def test_send_with_attachment_filename_in_disposition(tmp_path):
    """Content-Disposition must carry the file's basename."""
    ch = _make_channel()
    msg = _send_with_attachment(ch, tmp_path, b"hello", "alice-onboard.md")

    dispositions = [
        part.get("Content-Disposition", "")
        for part in msg.walk()
        if part.get("Content-Disposition")
    ]
    assert any("alice-onboard.md" in d for d in dispositions)


def test_send_without_attachments_unchanged(tmp_path):
    """send() with no attachments behaves exactly as before."""
    ch = _make_channel()
    mock_server = MagicMock()
    with patch("notifications.channels.smtp.smtplib.SMTP") as mock_smtp:
        mock_smtp.return_value.__enter__.return_value = mock_server
        result = ch.send("to@example.com", "No att", "Body")

    assert result.delivered is True
    mock_server.send_message.assert_called_once()
