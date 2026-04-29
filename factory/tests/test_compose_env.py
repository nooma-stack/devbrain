"""Tests for the _build_compose_env helper in project_cli."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from project_cli import _build_compose_env
from port_registry import PortRange


def _assignment(purpose: str, start: int, end: int = None):
    return SimpleNamespace(
        purpose=purpose,
        port_range=PortRange(start, end if end is not None else start),
    )


def test_build_env_single_port():
    assignments = [_assignment("api", 18000)]
    env = _build_compose_env(assignments)
    assert env == {"API_PORT": "18000"}


def test_build_env_multiple_single_ports():
    assignments = [
        _assignment("api", 18000),
        _assignment("web", 13000),
        _assignment("postgres", 15432),
    ]
    env = _build_compose_env(assignments)
    assert env == {
        "API_PORT": "18000",
        "WEB_PORT": "13000",
        "POSTGRES_PORT": "15432",
    }


def test_build_env_range_emits_start_end_no_port():
    assignments = [_assignment("asterisk_rtp", 20000, 20100)]
    env = _build_compose_env(assignments)
    assert env == {
        "ASTERISK_RTP_PORT_START": "20000",
        "ASTERISK_RTP_PORT_END": "20100",
    }
    assert "ASTERISK_RTP_PORT" not in env


def test_build_env_mixed_singles_and_ranges():
    assignments = [
        _assignment("api", 18000),
        _assignment("rtp", 20000, 20100),
    ]
    env = _build_compose_env(assignments)
    assert env["API_PORT"] == "18000"
    assert env["RTP_PORT_START"] == "20000"
    assert env["RTP_PORT_END"] == "20100"


def test_build_env_no_upper():
    assignments = [_assignment("api", 18000)]
    env = _build_compose_env(assignments, upper=False)
    assert env == {"api_PORT": "18000"}


def test_build_env_with_prefix():
    assignments = [_assignment("api", 18000)]
    env = _build_compose_env(assignments, prefix="DEVBRAIN_")
    assert env == {"DEVBRAIN_API_PORT": "18000"}


def test_build_env_replaces_hyphen_with_underscore():
    assignments = [_assignment("fake-carrier-sip", 15070)]
    env = _build_compose_env(assignments)
    assert env == {"FAKE_CARRIER_SIP_PORT": "15070"}


def test_build_env_empty_input_returns_empty():
    assert _build_compose_env([]) == {}
