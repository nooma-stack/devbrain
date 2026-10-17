"""Recall-audit metric + gold-set tests (no DB, no network).

Run from the ingest/ directory: ``cd ingest && python -m pytest tests/``
"""

from __future__ import annotations

import re

from recall_audit.audit import (
    DEFAULT_GOLD,
    first_hit_rank,
    index_loss,
    load_gold,
    merge_candidates,
    summarize,
)

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def test_first_hit_rank_uses_any_expected_id():
    assert first_hit_rank(["a", "b", "c"], ["c", "b"]) == 2
    assert first_hit_rank(["a"], ["z"]) is None


def test_summarize_recall_and_mrr():
    m = summarize([1, 4, 7, None])
    assert m.n == 4
    assert m.recall_at_5 == 0.5
    assert m.recall_at_10 == 0.75
    assert abs(m.mrr - (1 + 1 / 4 + 1 / 7) / 4) < 1e-12


def test_merge_matches_deep_search_resort():
    ranked = merge_candidates([("m1", 0.9), ("m2", 0.5)], [("", 0.7)], limit=2)
    assert ranked == ["m1", ""]


def test_index_loss_only_counts_exact_top10_hits():
    exact = {"g1": 3, "g2": 12, "g3": 1}
    index = {"g1": None, "g2": None, "g3": 1}
    assert index_loss(exact, index) == ["g1"]


def test_gold_set_is_well_formed():
    items = load_gold(DEFAULT_GOLD)
    assert len(items) >= 60
    kinds = {i.kind for i in items}
    assert {"decision", "pattern", "issue", "session_summary"} <= kinds
    assert len({i.project for i in items}) >= 5
    for it in items:
        assert it.query.strip()
        assert all(UUID.match(x) for x in it.expected_ids), it.id
    # The known hard case stays in the set.
    assert any("bc9df5f6-0172-482b-9f55-988f79bc3c12" in i.expected_ids for i in items)
