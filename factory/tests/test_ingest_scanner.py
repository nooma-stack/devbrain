"""Tests for ingest/main.py:_resolve_watch_dirs.

Covers two historical bugs:

1. Glob wildcards in watch_paths weren't expanded. openclaw's
   `~/.openclaw/agents/*/sessions` resolved to a literal Path with
   `*` as a directory name and the scanner reported it "not found".

2. markdown_memory uses `memory_dirs` (dict of {path: slug}) instead
   of `watch_paths` (list). The scanner only iterated `watch_paths`,
   silently skipping markdown_memory's directories.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add ingest/ to sys.path so we can import main.
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent.parent / "ingest")
)


def _call_resolve_with_config(cfg: dict, monkeypatch) -> list[Path]:
    """Helper: import _resolve_watch_dirs with ADAPTER_CONFIG monkey-patched.

    Re-imports the module so the closure picks up the patched config.
    """
    import importlib

    import config as ingest_config  # noqa: PLC0415

    monkeypatch.setattr(ingest_config, "ADAPTER_CONFIG", cfg)
    # Re-import main to pick up the patched ADAPTER_CONFIG at import time.
    if "main" in sys.modules:
        del sys.modules["main"]
    main = importlib.import_module("main")
    return main._resolve_watch_dirs()


def test_resolve_watch_dirs_expands_tilde(monkeypatch, tmp_path):
    """~ should expand to $HOME before any other processing."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = {
        "claude_code": {
            "enabled": True,
            "watch_paths": ["~/.claude/projects"],
        }
    }
    dirs = _call_resolve_with_config(cfg, monkeypatch)
    assert dirs == [tmp_path / ".claude" / "projects"]


def test_resolve_watch_dirs_skips_disabled(monkeypatch, tmp_path):
    cfg = {
        "claude_code": {"enabled": True, "watch_paths": [str(tmp_path / "a")]},
        "openclaw": {"enabled": False, "watch_paths": [str(tmp_path / "b")]},
    }
    dirs = _call_resolve_with_config(cfg, monkeypatch)
    assert tmp_path / "a" in dirs
    assert tmp_path / "b" not in dirs


def test_resolve_watch_dirs_expands_glob_wildcards(monkeypatch, tmp_path):
    """`~/.openclaw/agents/*/sessions` should resolve to every existing
    sessions/ dir under agents/, not a literal Path with `*` in it."""
    # Set up matching directories.
    for agent in ("main", "impl", "reviewer"):
        (tmp_path / "agents" / agent / "sessions").mkdir(parents=True)
    # Add a non-matching one (no sessions subdir) to ensure the glob
    # doesn't false-positive.
    (tmp_path / "agents" / "noisy").mkdir(parents=True)

    cfg = {
        "openclaw": {
            "enabled": True,
            "watch_paths": [str(tmp_path / "agents" / "*" / "sessions")],
        }
    }
    dirs = _call_resolve_with_config(cfg, monkeypatch)
    assert sorted(dirs) == sorted([
        tmp_path / "agents" / "impl" / "sessions",
        tmp_path / "agents" / "main" / "sessions",
        tmp_path / "agents" / "reviewer" / "sessions",
    ])


def test_resolve_watch_dirs_glob_with_no_matches_is_silent(monkeypatch, tmp_path):
    """A glob that matches nothing returns no dirs (doesn't raise)."""
    cfg = {
        "openclaw": {
            "enabled": True,
            "watch_paths": [str(tmp_path / "agents" / "*" / "sessions")],
        }
    }
    dirs = _call_resolve_with_config(cfg, monkeypatch)
    assert dirs == []


def test_resolve_watch_dirs_reads_memory_dirs_for_markdown_memory(
    monkeypatch, tmp_path
):
    """markdown_memory adapter uses memory_dirs={path: slug} instead of
    watch_paths. The scanner should still pick up its directories so it
    can traverse them."""
    (tmp_path / "notes").mkdir()
    cfg = {
        "markdown_memory": {
            "enabled": True,
            "memory_dirs": {str(tmp_path / "notes"): "myproject"},
        }
    }
    dirs = _call_resolve_with_config(cfg, monkeypatch)
    assert dirs == [tmp_path / "notes"]


def test_resolve_watch_dirs_handles_both_keys(monkeypatch, tmp_path):
    """An adapter with BOTH watch_paths and memory_dirs (rare but
    possible) should contribute all of them."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    cfg = {
        "weird": {
            "enabled": True,
            "watch_paths": [str(tmp_path / "a")],
            "memory_dirs": {str(tmp_path / "b"): "slug"},
        }
    }
    dirs = _call_resolve_with_config(cfg, monkeypatch)
    assert sorted(dirs) == sorted([tmp_path / "a", tmp_path / "b"])
