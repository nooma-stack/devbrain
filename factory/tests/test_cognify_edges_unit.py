"""cognify_edges helpers that need no DB: budget allocation across
projects, watermark encoding, seed selection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cognify import edges
from cognify.edges import _SeedGroup


def _g(n, key=None):
    return _SeedGroup(key=key, pairs=[(f"s{key}", f"o{i}") for i in range(n)])


def test_allocate_round_robin_shares_the_budget():
    big = [_g(3, k) for k in range(10)]
    small = [_g(2, 100), _g(2, 101)]
    alloc = edges._allocate({"big": big, "small": small}, 9)
    taken = {pid: sum(len(g.pairs) for g in gs) for pid, gs in alloc.items()}
    assert sum(taken.values()) == 9
    # The small project gets its turns instead of the big one taking all 9.
    assert taken == {"big": 6, "small": 3}


def test_allocate_marks_cut_group_incomplete_and_passes_free_groups():
    groups = [_g(0, 1), _g(3, 2), _g(0, 3), _g(3, 4)]
    alloc = edges._allocate({"p": groups}, 4)["p"]
    assert [len(g.pairs) for g in alloc] == [0, 3, 0, 1]
    assert [g.complete for g in alloc] == [True, True, True, False]


def test_allocate_rotates_first_turn():
    groups = {p: [_g(3, i) for i in range(3)] for p in ("a", "b", "c")}
    assert list(edges._allocate(groups, 3, start=0)) == ["a"]
    assert list(edges._allocate(groups, 3, start=1)) == ["b"]


def test_allocate_zero_budget_only_free_groups():
    alloc = edges._allocate({"p": [_g(0, 1), _g(2, 2), _g(0, 3)]}, 0)
    assert [len(g.pairs) for g in alloc["p"]] == [0]


def test_watermark_roundtrip():
    ts = datetime(2026, 10, 28, 4, 30, tzinfo=timezone.utc)
    wm = (ts, "0f9e")
    assert edges._wm_in(edges._wm_out(wm)) == wm
    assert edges._wm_in(None) is None
    assert edges._wm_in(["not-a-date", "x"]) is None


def test_seed_pool_filters_and_orders():
    now = datetime.now(timezone.utc)
    mems = [
        {"id": "b", "kind": "decision", "tier": "lesson", "created_at": now},
        {"id": "a", "kind": "pattern", "tier": "lesson", "created_at": now - timedelta(days=1)},
        {"id": "c", "kind": "session_summary", "tier": "memory", "created_at": now},
        {"id": "d", "kind": "issue", "tier": "memory", "created_at": now - timedelta(days=30)},
    ]
    pool = edges._seed_pool(mems, (now - timedelta(days=7), ""))
    assert [m["id"] for m in pool] == ["a", "b"]
    # Only non-atoms: fall back to everything (minus transcript chunks).
    only = [{"id": "x", "kind": "session_summary", "tier": "memory", "created_at": now},
            {"id": "y", "kind": "chunk", "tier": "memory", "created_at": now}]
    assert [m["id"] for m in edges._seed_pool(only, None)] == ["x"]
