"""Tests for factory.seed_ports — YAML parsing of dev-port-registry.yml."""
from __future__ import annotations

import pytest

from seed_ports import parse_registry


def test_parse_registry_empty():
    assert parse_registry("") == {}
    assert parse_registry("projects:\n") == {}


def test_parse_registry_single_project():
    yaml_text = """
projects:
  50tel-pbx:
    team: nooma-stack
    status: active
    path: /Users/patrickkelly/Nooma-Stack/50Tel PBX
    compose_project: 50tel-pbx-dev
    ports:
      api: 18000
      web: 13000
      asterisk_rtp: 20000-20100
"""
    result = parse_registry(yaml_text)
    assert "50tel-pbx" in result
    p = result["50tel-pbx"]
    assert p["team"] == "nooma-stack"
    assert p["status"] == "active"
    assert p["ports"]["api"] == 18000
    assert p["ports"]["web"] == 13000
    assert p["ports"]["asterisk_rtp"] == "20000-20100"


def test_parse_registry_multiple_projects():
    yaml_text = """
projects:
  proj-a:
    team: nooma-stack
    status: active
    ports:
      api: 18001
  proj-b:
    team: lhtdev
    status: archived
    ports:
      api: 28001
"""
    result = parse_registry(yaml_text)
    assert set(result.keys()) == {"proj-a", "proj-b"}
    assert result["proj-b"]["status"] == "archived"


def test_parse_registry_handles_no_projects_key():
    """If the YAML has top-level keys other than 'projects', return empty."""
    yaml_text = """
some_other_key: value
"""
    assert parse_registry(yaml_text) == {}


def test_parse_registry_handles_malformed_input():
    """Non-mapping top-level YAML returns empty rather than raising."""
    yaml_text = "- just\n- a list\n"
    assert parse_registry(yaml_text) == {}
