"""Tests for ai_clis.auth_helpers."""
from __future__ import annotations

import socket
from types import SimpleNamespace
from unittest.mock import patch

from ai_clis.auth_helpers import git_author_env, listener_on_port, verify_reverse_tunnel


def test_listener_on_port_returns_true_when_listening():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert listener_on_port(port) is True
    finally:
        s.close()


def test_listener_on_port_returns_false_when_no_listener():
    # Pick a port that's almost certainly closed
    assert listener_on_port(1) is False


def test_verify_reverse_tunnel_is_alias():
    with patch("ai_clis.auth_helpers.listener_on_port", return_value=True) as mock:
        assert verify_reverse_tunnel(12345) is True
        mock.assert_called_once_with(12345)


def test_git_author_env_uses_full_name_and_email():
    dev = SimpleNamespace(dev_id="alice", full_name="Alice Smith", email="alice@example.com")
    env = git_author_env(dev)
    assert env["GIT_AUTHOR_NAME"] == "Alice Smith"
    assert env["GIT_AUTHOR_EMAIL"] == "alice@example.com"
    assert env["GIT_COMMITTER_NAME"] == "Alice Smith"
    assert env["GIT_COMMITTER_EMAIL"] == "alice@example.com"


def test_git_author_env_falls_back_to_dev_id():
    dev = SimpleNamespace(dev_id="alice", full_name=None, email=None)
    env = git_author_env(dev)
    assert env["GIT_AUTHOR_NAME"] == "alice"
    assert env["GIT_AUTHOR_EMAIL"] == "alice@devbrain.local"


def test_git_author_env_handles_missing_attrs():
    dev = SimpleNamespace(dev_id="alice")
    env = git_author_env(dev)
    assert env["GIT_AUTHOR_NAME"] == "alice"
    assert "@devbrain.local" in env["GIT_AUTHOR_EMAIL"]
