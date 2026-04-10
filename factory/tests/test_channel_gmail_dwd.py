"""Tests for the Gmail DWD notification channel."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from notifications.channels.gmail_dwd import GmailDwdChannel


@pytest.fixture
def channel():
    return GmailDwdChannel(
        service_account_path="/tmp/fake-sa.json",
        sender_email="devbrain@example.com",
        sender_display_name="DevBrain",
    )


def test_is_configured_missing_file(channel):
    with patch("pathlib.Path.exists", return_value=False):
        assert channel.is_configured() is False


def test_is_configured_complete(channel):
    with patch("pathlib.Path.exists", return_value=True):
        assert channel.is_configured() is True


def test_is_configured_missing_sender():
    ch = GmailDwdChannel(
        service_account_path="/tmp/fake-sa.json",
        sender_email="",
    )
    with patch("pathlib.Path.exists", return_value=True):
        assert ch.is_configured() is False


def test_is_configured_missing_path():
    ch = GmailDwdChannel(
        service_account_path="",
        sender_email="devbrain@example.com",
    )
    assert ch.is_configured() is False


def test_send_success(channel):
    fake_service = MagicMock()
    fake_service.users.return_value.messages.return_value.send.return_value.execute.return_value = {
        "id": "msg-abc-123"
    }

    fake_creds = MagicMock()
    fake_creds.with_subject.return_value = fake_creds

    with patch("pathlib.Path.exists", return_value=True), patch(
        "notifications.channels.gmail_dwd.service_account"
    ) as mock_sa, patch(
        "notifications.channels.gmail_dwd.build", return_value=fake_service
    ) as mock_build:
        mock_sa.Credentials.from_service_account_file.return_value = fake_creds

        result = channel.send(
            "user@example.com",
            "Test Subject",
            "Test Body",
        )

    assert result.delivered is True
    assert result.channel == "gmail_dwd"
    assert result.metadata == {"message_id": "msg-abc-123"}
    assert result.error is None

    mock_sa.Credentials.from_service_account_file.assert_called_once()
    fake_creds.with_subject.assert_called_once_with("devbrain@example.com")
    mock_build.assert_called_once_with("gmail", "v1", credentials=fake_creds)
    fake_service.users.return_value.messages.return_value.send.assert_called_once()
    send_kwargs = fake_service.users.return_value.messages.return_value.send.call_args.kwargs
    assert send_kwargs["userId"] == "me"
    assert "raw" in send_kwargs["body"]


def test_send_api_error(channel):
    fake_service = MagicMock()
    fake_service.users.return_value.messages.return_value.send.return_value.execute.side_effect = (
        Exception("boom")
    )

    fake_creds = MagicMock()
    fake_creds.with_subject.return_value = fake_creds

    with patch("pathlib.Path.exists", return_value=True), patch(
        "notifications.channels.gmail_dwd.service_account"
    ) as mock_sa, patch(
        "notifications.channels.gmail_dwd.build", return_value=fake_service
    ):
        mock_sa.Credentials.from_service_account_file.return_value = fake_creds

        result = channel.send(
            "user@example.com",
            "Test Subject",
            "Test Body",
        )

    assert result.delivered is False
    assert result.channel == "gmail_dwd"
    assert result.error is not None
    assert "boom" in result.error


def test_send_not_configured():
    ch = GmailDwdChannel(service_account_path="", sender_email="")
    result = ch.send("user@example.com", "T", "B")
    assert result.delivered is False
    assert "not configured" in (result.error or "")
