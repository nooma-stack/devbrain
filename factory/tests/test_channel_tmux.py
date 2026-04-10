"""Tests for the TmuxChannel notification channel."""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from notifications.base import ChannelResult
from notifications.channels.tmux import TmuxChannel


def test_is_configured_when_tmux_present():
    with patch("notifications.channels.tmux.shutil.which", return_value="/usr/bin/tmux"):
        ch = TmuxChannel()
        assert ch.is_configured() is True


def test_is_configured_when_tmux_missing():
    with patch("notifications.channels.tmux.shutil.which", return_value=None):
        ch = TmuxChannel()
        assert ch.is_configured() is False


def test_send_no_session():
    ch = TmuxChannel()
    with patch.object(ch, "_is_session_active", return_value=False):
        result = ch.send("ghost-session", "Title", "Body")

    assert isinstance(result, ChannelResult)
    assert result.delivered is False
    assert result.channel == "tmux"
    assert "ghost-session" in (result.error or "")


def test_send_popup_success():
    ch = TmuxChannel()
    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.stderr = b""

    with patch.object(ch, "_is_session_active", return_value=True), \
         patch("notifications.channels.tmux.subprocess.run", return_value=fake_proc) as mock_run:
        result = ch.send("my-session", "Hello", "World")

    assert result.delivered is True
    assert result.channel == "tmux"
    assert result.error is None
    # Verify display-popup was invoked with the expected session target
    assert mock_run.called
    call_args = mock_run.call_args[0][0]
    assert call_args[0] == "tmux"
    assert "display-popup" in call_args
    assert "-t" in call_args
    assert "my-session" in call_args
    assert "-E" in call_args


def test_send_popup_nonzero_exit():
    ch = TmuxChannel()
    fake_proc = MagicMock()
    fake_proc.returncode = 1
    fake_proc.stderr = b"some tmux error"

    with patch.object(ch, "_is_session_active", return_value=True), \
         patch("notifications.channels.tmux.subprocess.run", return_value=fake_proc):
        result = ch.send("my-session", "Hello", "World")

    assert result.delivered is False
    assert result.channel == "tmux"
    assert "tmux exit 1" in (result.error or "")
    assert "some tmux error" in (result.error or "")


def test_send_handles_subprocess_error():
    ch = TmuxChannel()

    with patch.object(ch, "_is_session_active", return_value=True), \
         patch(
             "notifications.channels.tmux.subprocess.run",
             side_effect=subprocess.TimeoutExpired(cmd="tmux", timeout=10),
         ):
        result = ch.send("my-session", "Hello", "World")

    assert result.delivered is False
    assert result.channel == "tmux"
    assert result.error is not None
    assert "TimeoutExpired" in result.error


def test_is_session_active_handles_subprocess_error():
    ch = TmuxChannel()
    with patch(
        "notifications.channels.tmux.subprocess.run",
        side_effect=FileNotFoundError("tmux not found"),
    ):
        assert ch._is_session_active("any-session") is False


def test_format_contains_title_and_body():
    ch = TmuxChannel()
    formatted = ch._format("MyTitle", "MyBody")
    assert "MyTitle" in formatted
    assert "MyBody" in formatted
    assert "DevBrain" in formatted
