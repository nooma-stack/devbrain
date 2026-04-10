"""Tests for TelegramBotChannel."""
import json
from unittest.mock import MagicMock, patch

import pytest

from notifications.channels.telegram_bot import TelegramBotChannel


def _mock_response(data):
    fake_response = MagicMock()
    fake_response.read.return_value = json.dumps(data).encode()
    fake_response.__enter__ = lambda self: self
    fake_response.__exit__ = lambda *args: None
    return fake_response


def _make_channel(**overrides):
    defaults = dict(bot_token="test-token", bot_username="devbrain_bot")
    defaults.update(overrides)
    return TelegramBotChannel(**defaults)


def test_is_configured_with_token():
    ch = _make_channel()
    assert ch.is_configured() is True


def test_is_configured_no_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    ch = _make_channel(bot_token="")
    assert ch.is_configured() is False


def test_send_success():
    ch = _make_channel()
    payload = {"ok": True, "result": {"message_id": 42}}
    with patch(
        "notifications.channels.telegram_bot.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.return_value = _mock_response(payload)
        result = ch.send("123456", "Hello", "Body text")

    assert result.delivered is True
    assert result.channel == "telegram_bot"
    assert result.metadata == {"message_id": 42}
    mock_urlopen.assert_called_once()


def test_send_api_error():
    ch = _make_channel()
    payload = {"ok": False, "description": "chat not found"}
    with patch(
        "notifications.channels.telegram_bot.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.return_value = _mock_response(payload)
        result = ch.send("bogus", "Hello", "Body")

    assert result.delivered is False
    assert result.error == "chat not found"


def test_send_not_configured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    ch = _make_channel(bot_token="")
    result = ch.send("123456", "Hello", "Body")
    assert result.delivered is False
    assert result.error is not None


def test_discover_chat_id():
    ch = _make_channel()
    payload = {
        "ok": True,
        "result": [
            {
                "update_id": 1,
                "message": {
                    "chat": {
                        "id": 99887766,
                        "type": "private",
                        "username": "alice",
                    },
                    "text": "hi",
                },
            }
        ],
    }
    with patch(
        "notifications.channels.telegram_bot.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.return_value = _mock_response(payload)
        chat_id = ch.discover_chat_id()

    assert chat_id == "99887766"


def test_discover_with_username_hint():
    ch = _make_channel()
    payload = {
        "ok": True,
        "result": [
            {
                "update_id": 1,
                "message": {
                    "chat": {
                        "id": 111,
                        "type": "private",
                        "username": "alice",
                    },
                },
            },
            {
                "update_id": 2,
                "message": {
                    "chat": {
                        "id": 222,
                        "type": "private",
                        "username": "bob",
                    },
                },
            },
            {
                "update_id": 3,
                "message": {
                    "chat": {
                        "id": 333,
                        "type": "group",
                        "title": "Some Group",
                    },
                },
            },
        ],
    }
    with patch(
        "notifications.channels.telegram_bot.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.return_value = _mock_response(payload)
        chat_id = ch.discover_chat_id(username_hint="bob")

    assert chat_id == "222"


def test_discover_no_matches_returns_none():
    ch = _make_channel()
    payload = {"ok": True, "result": []}
    with patch(
        "notifications.channels.telegram_bot.urllib.request.urlopen"
    ) as mock_urlopen:
        mock_urlopen.return_value = _mock_response(payload)
        chat_id = ch.discover_chat_id()

    assert chat_id is None
