"""cognify_edges contradiction candidates: embedding neighbours, the
per-project watermark, and the once-per-run backend check (DB).

The judge is stubbed; embeddings are hand-built unit vectors so cosine
similarities are exact.
"""

from __future__ import annotations

import math
from unittest.mock import patch

import pytest
from cognify import edges
from cognify.edges import EDGE_TYPE_CONTRADICTS, EdgesPass

pytestmark = pytest.mark.db

DIM = 1024
NO_USAGE = {"input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0}


def vec(cos: float) -> str:
    """Unit vector at cosine ``cos`` from e0."""
    v = [0.0] * DIM
    v[0] = cos
    v[1] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return "[" + ",".join(str(x) for x in v) + "]"


def atom(conn, project_id, title, *, emb, offset_s, kind="decision"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory "
            "(project_id, kind, title, content, embedding, tier, created_at) "
            "VALUES (%s, %s, %s, %s, %s::vector, 'lesson', "
            "        now() - interval '1 hour' + make_interval(secs => %s)) "
            "RETURNING id",
            (project_id, kind, title, f"{title}: body text", emb, offset_s),
        )
        mid = cur.fetchone()[0]
    conn.commit()
    return mid


def contradicts(conn, a, b):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM devbrain.memory_dependencies "
            "WHERE from_memory_id = %s AND to_memory_id = %s AND edge_type = %s",
            (a, b, EDGE_TYPE_CONTRADICTS),
        )
        return cur.fetchone()[0]


class Judge:
    """Stub judge: says YES for pairs whose titles are in ``yes``."""

    def __init__(self, yes=()):
        self.yes = set(yes)
        self.calls = []

    def __call__(self, a, b, *, judges=None):
        self.calls.append((a.split(":")[0], b.split(":")[0]))
        return frozenset(self.calls[-1]) in self.yes, dict(NO_USAGE), None


@pytest.fixture
def usable_judge():
    with patch("cognify.backends.usable", return_value=(["codex"], {})):
        yield


def _run(conn, pid, judge, **kw):
    """One pass run, persisting metadata like the orchestrator would."""
    with patch("cognify.edges._llm_judge_contradiction", side_effect=judge):
        res = EdgesPass().run(conn, pid, **kw)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.cognify_run_log "
            "(pass_name, project_id, started_at, completed_at, metadata) "
            "VALUES ('edges', %s, clock_timestamp(), clock_timestamp(), %s::jsonb)",
            (pid, __import__("json").dumps(res.metadata)),
        )
    conn.commit()
    return res


@pytest.fixture
def run_log_cleanup(conn):
    yield
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM devbrain.cognify_run_log WHERE pass_name = 'edges' "
            "AND completed_at > now() - interval '1 hour'"
        )
    conn.commit()


def test_embedding_neighbours_become_pairs(conn, project_factory, usable_judge, run_log_cleanup):
    """Two near-duplicate atoms with no graph edge between them are judged
    (the old walk-only selection never saw them)."""
    p = project_factory("edges_emb")
    old = atom(conn, p["id"], "Use pooling", emb=vec(1.0), offset_s=0)
    new = atom(conn, p["id"], "Never pool", emb=vec(0.95), offset_s=10)
    atom(conn, p["id"], "Unrelated", emb=vec(0.10), offset_s=20)
    judge = Judge(yes={frozenset({"Use pooling", "Never pool"})})

    res = _run(conn, p["id"], judge)

    # Judged once, from the newer side; the dissimilar atom is not a pair.
    assert judge.calls == [("Never pool", "Use pooling")]
    assert contradicts(conn, new, old) == 1 and contradicts(conn, old, new) == 1
    assert res.metadata["pairs_judged"] == 1
    assert res.metadata["contradicts_edges_created"] == 2
    assert res.llm_calls == 1


def test_watermark_means_each_pair_is_judged_once(conn, project_factory, usable_judge, run_log_cleanup):
    p = project_factory("edges_wm")
    atom(conn, p["id"], "A", emb=vec(1.0), offset_s=0)
    atom(conn, p["id"], "B", emb=vec(0.97), offset_s=10)
    first = Judge()
    res1 = _run(conn, p["id"], first)
    assert first.calls == [("B", "A")]
    assert res1.metadata["watermark"] is not None

    second = Judge()
    res2 = _run(conn, p["id"], second)
    assert second.calls == []  # the old code re-judged the same pairs forever
    assert res2.metadata["pairs_judged"] == 0
    assert res2.metadata["watermark"] == res1.metadata["watermark"]

    atom(conn, p["id"], "C", emb=vec(0.96), offset_s=20)
    third = Judge()
    _run(conn, p["id"], third)
    assert [c[0] for c in third.calls] == ["C", "C"]


def test_budget_cut_keeps_unjudged_seeds_for_next_run(conn, project_factory, usable_judge, run_log_cleanup):
    p = project_factory("edges_budget")
    for i in range(6):
        atom(conn, p["id"], f"S{i}", emb=vec(1.0 - i * 0.01), offset_s=i)
    first = Judge()
    res = _run(conn, p["id"], first, max_llm_calls=4)
    assert len(first.calls) == 4
    assert res.metadata["seeds_pending"] > 0

    second = Judge()
    _run(conn, p["id"], second, max_llm_calls=15)
    # Nothing judged twice across the two runs, except the pairs of a seed
    # the budget cut part-way (re-judged whole on the next run).
    seen = set(first.calls)
    again = [c for c in second.calls if c in seen]
    assert len(again) <= edges.MAX_PAIRS_PER_SEED - 1


def test_no_usable_backend_blocks_without_judging(conn, project_factory, run_log_cleanup):
    p = project_factory("edges_blocked")
    atom(conn, p["id"], "A", emb=vec(1.0), offset_s=0)
    atom(conn, p["id"], "B", emb=vec(0.97), offset_s=10)
    judge = Judge()
    with patch(
        "cognify.backends.usable",
        return_value=([], {"codex": "no_auth: x", "anthropic": "no_auth: y"}),
    ):
        res = _run(conn, p["id"], judge)
    assert judge.calls == []
    assert res.metadata["blocked"] == "no_auth"
    assert res.metadata["seeds_pending"] == 2
    assert res.metadata["watermark"] is None
    assert res.llm_calls == 0


def test_judge_losing_auth_mid_run_stops(conn, project_factory, usable_judge, run_log_cleanup):
    p = project_factory("edges_dead")
    for i in range(4):
        atom(conn, p["id"], f"D{i}", emb=vec(1.0 - i * 0.01), offset_s=i)
    calls = []

    def dead(a, b, *, judges=None):
        calls.append(1)
        if judges is not None:
            judges.clear()
        return False, dict(NO_USAGE), "no_auth"

    with patch("cognify.edges._llm_judge_contradiction", side_effect=dead):
        res = EdgesPass().run(conn, p["id"])
    assert len(calls) == 1
    assert res.metadata["blocked"] == "no_auth"
    assert res.metadata["failure_counts"] == {"no_auth": 1}
    assert res.llm_calls == 0  # a refusal is not a model call


def test_settled_pairs_are_skipped(conn, project_factory, usable_judge, run_log_cleanup):
    p = project_factory("edges_settled")
    a = atom(conn, p["id"], "Old", emb=vec(1.0), offset_s=0)
    b = atom(conn, p["id"], "New", emb=vec(0.97), offset_s=10)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'supersedes', 'test')",
            (b, a),
        )
    conn.commit()
    judge = Judge()
    _run(conn, p["id"], judge)
    assert judge.calls == []
