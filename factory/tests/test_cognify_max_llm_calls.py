"""Tests for the --max-llm-calls CLI flag and its plumbing through the
orchestrator → ExtractPass / EdgesPass → _llm_extract / _detect_contradicts.

These tests focus on the cap *plumbing*, not the LLM call itself. The
LLM functions are stubbed; we assert how many times they get invoked.

Each pass also retains its built-in default cap (MAX_LLM_CALLS_PER_PASS),
exercised separately for sanity.
"""
from __future__ import annotations

import inspect
import os
from unittest.mock import patch

import pytest

from cognify import edges as edges_mod
from cognify import extract as extract_mod
from cognify.orchestrator import run_pass


# ─── Signatures: confirm the kwarg exists on every layer ─────────────────────


def test_extract_pass_run_accepts_max_llm_calls():
    sig = inspect.signature(extract_mod.ExtractPass.run)
    assert "max_llm_calls" in sig.parameters
    assert sig.parameters["max_llm_calls"].default is None


def test_edges_pass_run_accepts_max_llm_calls():
    sig = inspect.signature(edges_mod.EdgesPass.run)
    assert "max_llm_calls" in sig.parameters
    assert sig.parameters["max_llm_calls"].default is None


def test_run_edges_helper_accepts_max_llm_calls():
    sig = inspect.signature(edges_mod._run_edges)
    assert "max_llm_calls" in sig.parameters


def test_detect_contradicts_accepts_max_llm_calls():
    sig = inspect.signature(edges_mod._detect_contradicts)
    assert "max_llm_calls" in sig.parameters


def test_orchestrator_run_pass_accepts_max_llm_calls():
    sig = inspect.signature(run_pass)
    assert "max_llm_calls" in sig.parameters


# ─── Behavior: extract pass honors the override ──────────────────────────────


class _StubConn:
    """Minimal stand-in for psycopg2 conn — methods are unused because we
    stub `extract_from_session` before the conn is touched."""
    def cursor(self):
        raise RuntimeError("cursor() shouldn't be called when we stub the helpers")

    def commit(self):
        pass


def _set_extract_stubs(*, candidate_session_ids, per_session_lessons=1, per_session_decisions=1, per_session_llm=1):
    """Return patches that:
      - Make `_last_successful_run` return None.
      - Make `_sessions_since` return the given session id list.
      - Make `extract_from_session` return a deterministic _ExtractResult.

    Yields the list of patch context managers so the caller can stack
    them with contextlib.ExitStack if needed; this helper just returns
    them as a tuple.
    """
    from cognify.extract import ExtractResult  # noqa: PLC0415

    fake_result = ExtractResult(
        session_id="stub",
        lessons_created=per_session_lessons,
        decisions_created=per_session_decisions,
        llm_calls=per_session_llm,
    )
    return (
        patch("cognify.extract._last_successful_run", return_value=None),
        patch("cognify.extract._sessions_since", return_value=candidate_session_ids),
        patch("cognify.extract.extract_from_session", return_value=fake_result),
    )


def test_extract_pass_honors_max_llm_calls_below_default():
    """5 candidate sessions, each costs 1 LLM call. With max_llm_calls=3,
    we expect exactly 3 sessions processed (then break)."""
    sessions = [f"session-{i}" for i in range(5)]
    patches = _set_extract_stubs(candidate_session_ids=sessions)

    with patches[0], patches[1], patches[2] as p_extract:
        pass_obj = extract_mod.ExtractPass()
        result = pass_obj.run(_StubConn(), project_id="proj-1", max_llm_calls=3)

    assert result.llm_calls == 3
    # 3 sessions × 1 lesson + 3 × 1 decision = 6 rows
    assert result.rows_processed == 6
    assert p_extract.call_count == 3


def test_extract_pass_uses_default_cap_when_none():
    """max_llm_calls=None means use the module-level default (20).
    With 30 candidate sessions, we expect exactly 20 processed."""
    sessions = [f"session-{i}" for i in range(30)]
    patches = _set_extract_stubs(candidate_session_ids=sessions)

    with patches[0], patches[1], patches[2] as p_extract:
        pass_obj = extract_mod.ExtractPass()
        result = pass_obj.run(_StubConn(), project_id="proj-1", max_llm_calls=None)

    assert result.llm_calls == extract_mod.MAX_LLM_CALLS_PER_PASS
    assert p_extract.call_count == extract_mod.MAX_LLM_CALLS_PER_PASS


def test_extract_pass_zero_cap_skips_all_llm_work():
    """max_llm_calls=0 means do nothing LLM-side."""
    sessions = [f"session-{i}" for i in range(5)]
    patches = _set_extract_stubs(candidate_session_ids=sessions)

    with patches[0], patches[1], patches[2] as p_extract:
        pass_obj = extract_mod.ExtractPass()
        result = pass_obj.run(_StubConn(), project_id="proj-1", max_llm_calls=0)

    assert result.llm_calls == 0
    assert p_extract.call_count == 0


# ─── Behavior: edges pass honors the override ────────────────────────────────


def test_detect_contradicts_honors_max_llm_calls_below_default():
    """Stub the LLM judge so it always returns "not a contradiction" and
    bump candidate-pair generation so we have plenty. Then assert the
    LLM is called exactly `cap` times when cap < default (15)."""
    from uuid import uuid4
    # Build 20 fake memories — more than the default cap.
    memories = [
        {
            "id": uuid4(),
            "kind": "memory",
            "tier": "lesson",
            "title": f"Lesson {i}",
            "content": f"Lesson body {i}",
        }
        for i in range(20)
    ]

    fake_call_count = {"n": 0}

    def fake_llm_judge(content_a, content_b):
        fake_call_count["n"] += 1
        return False, {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }

    # Fake a graph_walk that returns all other memories as neighbors,
    # so candidate_pairs >= 100 (way over any cap we'd test).
    class FakeNode:
        def __init__(self, mid):
            self.id = mid

    class FakeWalkResult:
        def __init__(self, nodes):
            self.memories = nodes

    def fake_graph_walk(*args, **kwargs):
        all_ids = [m["id"] for m in memories]
        return FakeWalkResult([FakeNode(mid) for mid in all_ids])

    with patch("cognify.edges._load_memories", return_value=memories), \
         patch("cognify.edges._llm_judge_contradiction", side_effect=fake_llm_judge), \
         patch("cognify.edges.walk", side_effect=fake_graph_walk), \
         patch("cognify.edges._insert_edge", return_value=1):
        new_edges, llm_calls = edges_mod._detect_contradicts(
            conn=_StubConn(),
            project_id="proj-1",
            dry_run=False,
            record_conn=None,
            max_llm_calls=4,
        )

    # The cap should be honored exactly.
    assert llm_calls == 4
    assert fake_call_count["n"] == 4


def test_detect_contradicts_uses_default_cap_when_none():
    """Same setup, max_llm_calls=None → uses MAX_LLM_CALLS_PER_PASS=15."""
    from uuid import uuid4
    memories = [
        {
            "id": uuid4(),
            "kind": "memory",
            "tier": "lesson",
            "title": f"Lesson {i}",
            "content": f"Lesson body {i}",
        }
        for i in range(40)
    ]

    fake_call_count = {"n": 0}

    def fake_llm_judge(content_a, content_b):
        fake_call_count["n"] += 1
        return False, {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }

    class FakeNode:
        def __init__(self, mid):
            self.id = mid

    class FakeWalkResult:
        def __init__(self, nodes):
            self.memories = nodes

    def fake_graph_walk(*args, **kwargs):
        return FakeWalkResult([FakeNode(m["id"]) for m in memories])

    with patch("cognify.edges._load_memories", return_value=memories), \
         patch("cognify.edges._llm_judge_contradiction", side_effect=fake_llm_judge), \
         patch("cognify.edges.walk", side_effect=fake_graph_walk), \
         patch("cognify.edges._insert_edge", return_value=1):
        new_edges, llm_calls = edges_mod._detect_contradicts(
            conn=_StubConn(),
            project_id="proj-1",
            dry_run=False,
            record_conn=None,
            max_llm_calls=None,
        )

    assert llm_calls == edges_mod.MAX_LLM_CALLS_PER_PASS  # 15


def test_detect_contradicts_zero_cap_skips_all_llm_work():
    """max_llm_calls=0 means no LLM calls."""
    from uuid import uuid4
    memories = [
        {
            "id": uuid4(),
            "kind": "memory",
            "tier": "lesson",
            "title": f"Lesson {i}",
            "content": f"Lesson body {i}",
        }
        for i in range(10)
    ]

    fake_call_count = {"n": 0}

    def fake_llm_judge(content_a, content_b):
        fake_call_count["n"] += 1
        return False, {}

    class FakeNode:
        def __init__(self, mid):
            self.id = mid

    class FakeWalkResult:
        def __init__(self, nodes):
            self.memories = nodes

    def fake_graph_walk(*args, **kwargs):
        return FakeWalkResult([FakeNode(m["id"]) for m in memories])

    with patch("cognify.edges._load_memories", return_value=memories), \
         patch("cognify.edges._llm_judge_contradiction", side_effect=fake_llm_judge), \
         patch("cognify.edges.walk", side_effect=fake_graph_walk), \
         patch("cognify.edges._insert_edge", return_value=1):
        new_edges, llm_calls = edges_mod._detect_contradicts(
            conn=_StubConn(),
            project_id="proj-1",
            dry_run=False,
            record_conn=None,
            max_llm_calls=0,
        )

    assert llm_calls == 0
    assert fake_call_count["n"] == 0
