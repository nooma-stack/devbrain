"""Tests for webhook notification channels (Slack, Discord, generic)."""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from notifications.channels.webhook_slack import WebhookSlackChannel
from notifications.channels.webhook_discord import WebhookDiscordChannel
from notifications.channels.webhook_generic import WebhookGenericChannel


def _mock_ok():
    fake_response = MagicMock()
    fake_response.read.return_value = b"ok"
    fake_response.getcode.return_value = 200
    fake_response.__enter__ = lambda self: self
    fake_response.__exit__ = lambda *args: None
    return fake_response


def _payload_from_call(mock_urlopen):
    """Extract and JSON-decode the POST body from the mocked urlopen call."""
    req = mock_urlopen.call_args[0][0]
    return json.loads(req.data.decode("utf-8"))


def test_slack_sends_text_field():
    channel = WebhookSlackChannel()
    with patch("notifications.channels._webhook_base.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_ok()
        result = channel.send(
            "https://hooks.slack.com/services/XXX",
            "Hello",
            "World body",
        )
    assert result.delivered is True
    assert result.channel == "webhook_slack"
    payload = _payload_from_call(mock_urlopen)
    assert "text" in payload
    assert "Hello" in payload["text"]
    assert "World body" in payload["text"]


def test_discord_sends_content_field():
    channel = WebhookDiscordChannel()
    with patch("notifications.channels._webhook_base.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_ok()
        result = channel.send(
            "https://discord.com/api/webhooks/XXX/YYY",
            "Title",
            "Body text",
        )
    assert result.delivered is True
    assert result.channel == "webhook_discord"
    payload = _payload_from_call(mock_urlopen)
    assert "content" in payload
    assert "Title" in payload["content"]
    assert "Body text" in payload["content"]


def test_generic_sends_title_body_fields():
    channel = WebhookGenericChannel()
    with patch("notifications.channels._webhook_base.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_ok()
        result = channel.send(
            "https://example.com/hook",
            "My Title",
            "My Body",
            event_type="test_event",
        )
    assert result.delivered is True
    assert result.channel == "webhook_generic"
    payload = _payload_from_call(mock_urlopen)
    assert payload["title"] == "My Title"
    assert payload["body"] == "My Body"
    assert payload["event_type"] == "test_event"
    assert "timestamp" in payload
    # timestamp should be parseable ISO format
    assert "T" in payload["timestamp"]


def test_webhook_handles_http_error():
    channel = WebhookSlackChannel()
    with patch("notifications.channels._webhook_base.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://hooks.slack.com/services/XXX",
            400,
            "Bad Request",
            {},
            None,
        )
        result = channel.send(
            "https://hooks.slack.com/services/XXX",
            "Title",
            "Body",
        )
    assert result.delivered is False
    assert result.channel == "webhook_slack"
    assert "400" in result.error


def test_webhook_handles_generic_error():
    channel = WebhookDiscordChannel()
    with patch("notifications.channels._webhook_base.urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = RuntimeError("connection refused")
        result = channel.send(
            "https://discord.com/api/webhooks/XXX",
            "Title",
            "Body",
        )
    assert result.delivered is False
    assert result.channel == "webhook_discord"
    assert "RuntimeError" in result.error or "connection refused" in result.error
