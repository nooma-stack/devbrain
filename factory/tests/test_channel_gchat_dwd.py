"""Tests for GchatDwdChannel."""
from unittest.mock import MagicMock, patch

import pytest

from notifications.channels.gchat_dwd import GchatDwdChannel


def _make_channel(**overrides):
    defaults = dict(
        service_account_path="/fake/path/sa.json",
        sender_email="devbrain@example.com",
        auto_create_dm_space=True,
    )
    defaults.update(overrides)
    return GchatDwdChannel(**defaults)


def test_is_configured_missing_file():
    ch = _make_channel()
    with patch("notifications.channels.gchat_dwd.GOOGLE_AVAILABLE", True), patch(
        "notifications.channels.gchat_dwd.Path"
    ) as mock_path:
        mock_path.return_value.exists.return_value = False
        assert ch.is_configured() is False


def test_is_configured_complete():
    ch = _make_channel()
    with patch("notifications.channels.gchat_dwd.GOOGLE_AVAILABLE", True), patch(
        "notifications.channels.gchat_dwd.Path"
    ) as mock_path:
        mock_path.return_value.exists.return_value = True
        assert ch.is_configured() is True


def test_is_configured_missing_sender():
    ch = _make_channel(sender_email="")
    with patch("notifications.channels.gchat_dwd.GOOGLE_AVAILABLE", True), patch(
        "notifications.channels.gchat_dwd.Path"
    ) as mock_path:
        mock_path.return_value.exists.return_value = True
        assert ch.is_configured() is False


def test_is_configured_missing_path():
    ch = _make_channel(service_account_path="")
    with patch("notifications.channels.gchat_dwd.GOOGLE_AVAILABLE", True):
        assert ch.is_configured() is False


def test_send_to_existing_space_id():
    ch = _make_channel()
    mock_service = MagicMock()
    mock_service.spaces.return_value.messages.return_value.create.return_value.execute.return_value = {
        "name": "spaces/ABC/messages/XYZ"
    }

    with patch("notifications.channels.gchat_dwd.GOOGLE_AVAILABLE", True), patch(
        "notifications.channels.gchat_dwd.Path"
    ) as mock_path, patch(
        "notifications.channels.gchat_dwd.service_account"
    ), patch(
        "notifications.channels.gchat_dwd.build", return_value=mock_service
    ):
        mock_path.return_value.exists.return_value = True
        result = ch.send("spaces/ABC", "Hello", "Body text")

    assert result.delivered is True
    assert result.channel == "gchat_dwd"
    assert result.metadata["space_id"] == "spaces/ABC"
    assert result.metadata["message_id"] == "spaces/ABC/messages/XYZ"

    # setup() should NOT be called because address is a space ID
    mock_service.spaces.return_value.setup.assert_not_called()
    # messages.create called with the pre-supplied space id
    create_call = mock_service.spaces.return_value.messages.return_value.create
    create_call.assert_called_once()
    kwargs = create_call.call_args.kwargs
    assert kwargs["parent"] == "spaces/ABC"
    assert "Hello" in kwargs["body"]["text"]
    assert "Body text" in kwargs["body"]["text"]


def test_send_to_email_auto_creates_dm():
    ch = _make_channel()
    mock_service = MagicMock()
    # spaces.setup response
    mock_service.spaces.return_value.setup.return_value.execute.return_value = {
        "name": "spaces/NEW_DM"
    }
    # messages.create response
    mock_service.spaces.return_value.messages.return_value.create.return_value.execute.return_value = {
        "name": "spaces/NEW_DM/messages/MSG1"
    }

    with patch("notifications.channels.gchat_dwd.GOOGLE_AVAILABLE", True), patch(
        "notifications.channels.gchat_dwd.Path"
    ) as mock_path, patch(
        "notifications.channels.gchat_dwd.service_account"
    ), patch(
        "notifications.channels.gchat_dwd.build", return_value=mock_service
    ):
        mock_path.return_value.exists.return_value = True
        result = ch.send("alice@example.com", "Hi Alice", "Hello there")

    assert result.delivered is True
    assert result.metadata["space_id"] == "spaces/NEW_DM"

    # setup() was invoked to create the DM space
    setup_call = mock_service.spaces.return_value.setup
    setup_call.assert_called_once()
    setup_body = setup_call.call_args.kwargs["body"]
    assert setup_body["space"]["spaceType"] == "DIRECT_MESSAGE"
    assert setup_body["memberships"][0]["member"]["name"] == "users/alice@example.com"
    assert setup_body["memberships"][0]["member"]["type"] == "HUMAN"

    # Message sent to the newly created space
    create_call = mock_service.spaces.return_value.messages.return_value.create
    create_call.assert_called_once()
    assert create_call.call_args.kwargs["parent"] == "spaces/NEW_DM"

    # Cache should remember the created space
    assert ch._email_to_space["alice@example.com"] == "spaces/NEW_DM"


def test_send_uses_cached_dm_space():
    ch = _make_channel()
    ch._email_to_space["bob@example.com"] = "spaces/CACHED"

    mock_service = MagicMock()
    mock_service.spaces.return_value.messages.return_value.create.return_value.execute.return_value = {
        "name": "spaces/CACHED/messages/M1"
    }

    with patch("notifications.channels.gchat_dwd.GOOGLE_AVAILABLE", True), patch(
        "notifications.channels.gchat_dwd.Path"
    ) as mock_path, patch(
        "notifications.channels.gchat_dwd.service_account"
    ), patch(
        "notifications.channels.gchat_dwd.build", return_value=mock_service
    ):
        mock_path.return_value.exists.return_value = True
        result = ch.send("bob@example.com", "Hey", "Body")

    assert result.delivered is True
    assert result.metadata["space_id"] == "spaces/CACHED"
    # setup() must not be called when cache hits
    mock_service.spaces.return_value.setup.assert_not_called()


def test_send_invalid_address():
    ch = _make_channel()
    with patch("notifications.channels.gchat_dwd.GOOGLE_AVAILABLE", True), patch(
        "notifications.channels.gchat_dwd.Path"
    ) as mock_path, patch(
        "notifications.channels.gchat_dwd.service_account"
    ), patch(
        "notifications.channels.gchat_dwd.build"
    ):
        mock_path.return_value.exists.return_value = True
        result = ch.send("not-an-email-or-space", "T", "B")

    assert result.delivered is False
    assert result.error is not None
    assert "Invalid address" in result.error


def test_send_api_error():
    ch = _make_channel()
    mock_service = MagicMock()
    mock_service.spaces.return_value.messages.return_value.create.return_value.execute.side_effect = Exception(
        "Boom"
    )

    with patch("notifications.channels.gchat_dwd.GOOGLE_AVAILABLE", True), patch(
        "notifications.channels.gchat_dwd.Path"
    ) as mock_path, patch(
        "notifications.channels.gchat_dwd.service_account"
    ), patch(
        "notifications.channels.gchat_dwd.build", return_value=mock_service
    ):
        mock_path.return_value.exists.return_value = True
        result = ch.send("spaces/ABC", "Hello", "Body")

    assert result.delivered is False
    assert result.error is not None
    assert "Boom" in result.error


def test_send_not_configured_returns_error():
    ch = _make_channel(service_account_path="")
    result = ch.send("spaces/ABC", "T", "B")
    assert result.delivered is False
    assert result.error is not None
