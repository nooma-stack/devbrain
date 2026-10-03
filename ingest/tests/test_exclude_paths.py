"""Ingest path exclusion tests (no DB, no network).

Run from the ingest/ directory: ``cd ingest && python -m pytest tests/``
"""

from __future__ import annotations

from pathlib import Path

import pipeline
from pipeline import ingest_file, is_excluded_path

RUNNER_DIR = "-Users-lhtdev-brightbot-triage"


def test_subagent_transcripts_are_excluded_by_default():
    path = Path("/Users/dev/.claude/projects/-Users-dev-app/abc123/subagents/agent-1.jsonl")
    assert is_excluded_path(path)


def test_drop_zone_subagent_transcripts_are_excluded():
    path = Path("/Users/dev/ingest-incoming/mark/projects/-Users-mark-app/abc/subagents/agent-2.jsonl")
    assert is_excluded_path(path)


def test_configured_project_dir_is_excluded():
    path = Path(f"/Users/lhtdev/.claude/projects/{RUNNER_DIR}/sess.jsonl")
    assert is_excluded_path(path, ["subagents", RUNNER_DIR])


def test_ordinary_session_is_not_excluded():
    path = Path("/Users/dev/.claude/projects/-Users-dev-app/sess.jsonl")
    assert not is_excluded_path(path, ["subagents", RUNNER_DIR])


def test_matches_whole_path_components_only():
    path = Path("/Users/dev/.claude/projects/-Users-dev-subagents-notes/sess.jsonl")
    assert not is_excluded_path(path, ["subagents"])


def test_empty_list_excludes_nothing():
    path = Path("/Users/dev/.claude/projects/-Users-dev-app/abc/subagents/agent-1.jsonl")
    assert not is_excluded_path(path, [])


def test_ingest_file_skips_excluded_path_before_reading_it(monkeypatch, tmp_path):
    path = tmp_path / "abc" / "subagents" / "agent-1.jsonl"

    def _fail(_path):
        raise AssertionError("excluded file must not reach adapter detection")

    monkeypatch.setattr(pipeline, "detect_adapter", _fail)
    assert ingest_file(path) is False
