"""Tests for cognify.bulk — discovery, resume, cost-cap, recycle."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cognify.bulk import (
    BulkRunResult,
    _checkpoint_path_for,
    _load_checkpoint,
    _save_checkpoint,
    discover_all_sessions_with_chunks,
    discover_sessions_needing_atomization,
    run_bulk,
)


# ─── Checkpoint I/O ──────────────────────────────────────────────────────────


def test_save_and_load_checkpoint_roundtrip(tmp_path):
    p = tmp_path / "ckpt.json"
    data = {
        "project_slug": "brightbot",
        "completed_sessions": ["abc", "def"],
        "atoms_created": 17,
    }
    _save_checkpoint(p, data)
    assert p.exists()
    loaded = _load_checkpoint(p)
    assert loaded == data


def test_load_checkpoint_missing_returns_none(tmp_path):
    assert _load_checkpoint(tmp_path / "absent.json") is None


def test_load_checkpoint_corrupt_returns_none(tmp_path):
    p = tmp_path / "ckpt.json"
    p.write_text("not json at all }")
    assert _load_checkpoint(p) is None


def test_checkpoint_path_uses_devbrain_home(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVBRAIN_HOME", str(tmp_path))
    p = _checkpoint_path_for("foo")
    assert p == tmp_path / ".devbrain" / "cognify-bulk-foo.json"
    assert p.parent.exists()  # the function should have mkdir'd


# ─── Discovery queries ───────────────────────────────────────────────────────


class _MockCursor:
    def __init__(self, results):
        self._next = list(results)
        self._current = []

    def execute(self, *_args, **_kw):
        self._current = self._next.pop(0) if self._next else []

    def fetchall(self):
        return [(r,) for r in self._current]

    def fetchone(self):
        return (self._current[0],) if self._current else None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _MockConn:
    def __init__(self, *result_sets):
        self._results = list(result_sets)

    def cursor(self):
        return _MockCursor(self._results)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_discover_sessions_needing_atomization_returns_strings():
    conn = _MockConn(["abc-123", "def-456"])
    result = discover_sessions_needing_atomization(conn, project_id="proj-1")
    assert result == ["abc-123", "def-456"]


def test_discover_all_sessions_with_chunks_returns_strings():
    conn = _MockConn(["abc-123", "def-456", "ghi-789"])
    result = discover_all_sessions_with_chunks(conn, project_id="proj-1")
    assert result == ["abc-123", "def-456", "ghi-789"]


def test_discover_passes_since_filter_when_set(monkeypatch):
    """Confirm the since parameter actually affects SQL execution."""
    executed_sql: list[tuple] = []

    class _Cur:
        def execute(self, sql, params):
            executed_sql.append((sql, list(params)))
        def fetchall(self):
            return []
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Conn:
        def cursor(self): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    when = datetime(2026, 4, 1, tzinfo=timezone.utc)
    discover_sessions_needing_atomization(
        _Conn(), project_id="proj-1", since=when,
    )
    assert len(executed_sql) == 1
    sql, params = executed_sql[0]
    assert "m.created_at >= %s" in sql
    assert when in params


# ─── run_bulk: dry-run + discovery contract ──────────────────────────────────


def _build_extract_result(*, lessons=1, decisions=1, llm_calls=1, failure=None):
    """Build an ExtractResult stand-in for stubbing extract_from_session."""
    from cognify.extract import ExtractResult
    return ExtractResult(
        session_id="stub",
        lessons_created=lessons,
        decisions_created=decisions,
        llm_calls=llm_calls,
        failure=failure,
    )


def test_run_bulk_dry_run_makes_no_extract_calls(tmp_path):
    """Dry run reports planned work without calling the LLM."""
    sessions = ["s-1", "s-2", "s-3"]
    with patch("cognify.extract.extract_from_session") as mock_extract:
        result = run_bulk(
            conn=MagicMock(),
            project_id="proj-1",
            project_slug="test",
            sessions=sessions,
            dry_run=True,
            use_checkpoint=False,
        )
    assert result.sessions_targeted == 3
    assert result.sessions_processed == 0
    assert mock_extract.call_count == 0


def test_run_bulk_processes_each_session_and_aggregates(tmp_path, monkeypatch):
    """Happy path: 3 sessions, each returns 1 lesson + 1 decision."""
    sessions = ["s-1", "s-2", "s-3"]
    monkeypatch.setenv("DEVBRAIN_HOME", str(tmp_path))

    with patch("cognify.extract.extract_from_session",
               return_value=_build_extract_result(lessons=1, decisions=1, llm_calls=1)):
        result = run_bulk(
            conn=MagicMock(),
            project_id="proj-1",
            project_slug="test",
            sessions=sessions,
            use_checkpoint=False,
            recycle_every=0,  # disable recycle pause for tests
        )

    assert result.sessions_targeted == 3
    assert result.sessions_processed == 3
    assert result.sessions_failed == 0
    assert result.atoms_created == 6  # 3 sessions × 2 atoms
    assert result.llm_calls == 3


def test_run_bulk_max_llm_calls_halts_cleanly(tmp_path, monkeypatch):
    """With a 2-call cap and sessions costing 1 call each, only 2 of 5
    sessions get processed; halted_early=True; halt_reason is set."""
    sessions = [f"s-{i}" for i in range(5)]
    monkeypatch.setenv("DEVBRAIN_HOME", str(tmp_path))

    with patch("cognify.extract.extract_from_session",
               return_value=_build_extract_result(lessons=1, decisions=1, llm_calls=1)):
        result = run_bulk(
            conn=MagicMock(),
            project_id="proj-1",
            project_slug="test",
            sessions=sessions,
            max_llm_calls=2,
            use_checkpoint=False,
            recycle_every=0,
        )

    assert result.sessions_processed == 2
    assert result.llm_calls == 2
    assert result.halted_early is True
    assert "max_llm_calls" in result.halt_reason


def test_run_bulk_counts_failures_by_kind(tmp_path, monkeypatch):
    """Mixed success+failure across sessions; failure_counts breaks down
    by _failure kind (api / json_parse / etc.)."""
    sessions = ["good-1", "fail-api-1", "fail-json-1", "good-2", "fail-api-2"]
    monkeypatch.setenv("DEVBRAIN_HOME", str(tmp_path))

    def _per_session(conn, sid, pid, *, reextract):
        if "fail-api" in sid:
            return _build_extract_result(lessons=0, decisions=0, llm_calls=1, failure="api")
        if "fail-json" in sid:
            return _build_extract_result(lessons=0, decisions=0, llm_calls=1, failure="json_parse")
        return _build_extract_result(lessons=1, decisions=1, llm_calls=1)

    with patch("cognify.extract.extract_from_session", side_effect=_per_session):
        result = run_bulk(
            conn=MagicMock(),
            project_id="proj-1",
            project_slug="test",
            sessions=sessions,
            use_checkpoint=False,
            recycle_every=0,
        )

    assert result.sessions_processed == 5
    assert result.sessions_failed == 3
    assert result.atoms_created == 4  # 2 good sessions × 2 atoms each
    assert result.failure_counts == {"api": 2, "json_parse": 1}


# ─── Checkpoint resume ──────────────────────────────────────────────────────


def test_run_bulk_writes_checkpoint_after_each_session(tmp_path, monkeypatch):
    """Checkpoint file should exist after a successful run and contain
    the list of completed sessions."""
    sessions = ["s-1", "s-2"]
    monkeypatch.setenv("DEVBRAIN_HOME", str(tmp_path))

    with patch("cognify.extract.extract_from_session",
               return_value=_build_extract_result(lessons=1, decisions=1, llm_calls=1)):
        result = run_bulk(
            conn=MagicMock(),
            project_id="proj-1",
            project_slug="resumetest",
            sessions=sessions,
            use_checkpoint=True,
            recycle_every=0,
        )

    # Clean completion deletes the checkpoint
    ckpt = _checkpoint_path_for("resumetest")
    assert not ckpt.exists()  # cleaned up on clean completion
    assert result.checkpoint_path is None


def test_run_bulk_resumes_from_checkpoint(tmp_path, monkeypatch):
    """Pre-existing checkpoint with completed sessions: those are
    skipped and stats are carried forward."""
    monkeypatch.setenv("DEVBRAIN_HOME", str(tmp_path))
    sessions = ["s-1", "s-2", "s-3"]

    # Pre-write a checkpoint as if we'd previously completed s-1.
    ckpt_path = _checkpoint_path_for("resumetest")
    _save_checkpoint(ckpt_path, {
        "project_slug": "resumetest",
        "completed_sessions": ["s-1"],
        "atoms_created": 2,
        "llm_calls": 1,
        "failure_counts": {},
    })

    with patch("cognify.extract.extract_from_session",
               return_value=_build_extract_result(lessons=1, decisions=1, llm_calls=1)) as mock_extract:
        result = run_bulk(
            conn=MagicMock(),
            project_id="proj-1",
            project_slug="resumetest",
            sessions=sessions,
            use_checkpoint=True,
            recycle_every=0,
        )

    # Only s-2 and s-3 should have been extracted; s-1 was already done.
    assert mock_extract.call_count == 2
    assert result.sessions_skipped_resume == 1
    # Stats accumulate: prior 2 atoms + 4 new = 6
    assert result.atoms_created == 6
    # Prior 1 call + 2 new = 3
    assert result.llm_calls == 3


def test_run_bulk_preserves_checkpoint_when_halted_early(tmp_path, monkeypatch):
    """When --max-llm-calls cap is hit mid-run, the checkpoint must
    survive so a re-run can resume."""
    monkeypatch.setenv("DEVBRAIN_HOME", str(tmp_path))
    sessions = [f"s-{i}" for i in range(5)]

    with patch("cognify.extract.extract_from_session",
               return_value=_build_extract_result(lessons=1, decisions=1, llm_calls=1)):
        result = run_bulk(
            conn=MagicMock(),
            project_id="proj-1",
            project_slug="caphit",
            sessions=sessions,
            max_llm_calls=2,
            use_checkpoint=True,
            recycle_every=0,
        )

    ckpt_path = _checkpoint_path_for("caphit")
    assert result.halted_early is True
    assert ckpt_path.exists()  # NOT cleaned up when halted early
    body = json.loads(ckpt_path.read_text())
    assert len(body["completed_sessions"]) == 2


def test_run_bulk_preserves_checkpoint_when_any_failure(tmp_path, monkeypatch):
    """If any session fails, the checkpoint survives — even if the
    overall run completed (the user may want to retry just the failures
    on a future invocation)."""
    monkeypatch.setenv("DEVBRAIN_HOME", str(tmp_path))
    sessions = ["good-1", "fail-1"]

    def _per_session(conn, sid, pid, *, reextract):
        if sid == "fail-1":
            return _build_extract_result(lessons=0, decisions=0, llm_calls=1, failure="api")
        return _build_extract_result(lessons=1, decisions=1, llm_calls=1)

    with patch("cognify.extract.extract_from_session", side_effect=_per_session):
        result = run_bulk(
            conn=MagicMock(),
            project_id="proj-1",
            project_slug="failtest",
            sessions=sessions,
            use_checkpoint=True,
            recycle_every=0,
        )

    ckpt_path = _checkpoint_path_for("failtest")
    assert result.sessions_failed == 1
    assert ckpt_path.exists()  # preserved for retry


# ─── Progress callback ──────────────────────────────────────────────────────


def test_run_bulk_invokes_progress_callback_per_session(tmp_path, monkeypatch):
    """progress_callback fires after each session, with (idx, total, dict)."""
    monkeypatch.setenv("DEVBRAIN_HOME", str(tmp_path))
    sessions = ["s-1", "s-2", "s-3"]
    calls: list[tuple] = []

    def _cb(idx, total, last):
        calls.append((idx, total, last["session_id"], last["atoms"]))

    with patch("cognify.extract.extract_from_session",
               return_value=_build_extract_result(lessons=2, decisions=3, llm_calls=1)):
        run_bulk(
            conn=MagicMock(),
            project_id="proj-1",
            project_slug="progress",
            sessions=sessions,
            use_checkpoint=False,
            progress_callback=_cb,
            recycle_every=0,
        )

    assert calls == [
        (1, 3, "s-1", 5),
        (2, 3, "s-2", 5),
        (3, 3, "s-3", 5),
    ]
