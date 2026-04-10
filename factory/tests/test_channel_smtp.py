"""Tests for SmtpChannel."""
import os
import smtplib
from unittest.mock import patch, MagicMock

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
