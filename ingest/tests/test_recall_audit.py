"""Recall-audit metric + gold-set tests (no DB, no network).

Run from the ingest/ directory: ``cd ingest && python -m pytest tests/``
"""

from __future__ import annotations

import re
from pathlib import Path

from recall_audit.audit import (
    DEFAULT_GOLD,
    KEYWORD_SQL_PATH,
    HybridParams,
    first_hit_rank,
    index_loss,
    load_gold,
    load_keyword_sql,
    merge_candidates,
    percentile,
    rrf_fuse,
    summarize,
)

HYBRID_TS = Path(__file__).resolve().parents[2] / "mcp-server" / "src" / "hybrid.ts"

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


def test_rrf_fuse_matches_mcp():
    # Same case as 'matches the Python audit mirror' in mcp-server/src/hybrid.test.ts.
    fused = rrf_fuse(
        [["m1", "m2", "m3", "m4"], ["m4", "m9", "m1"]],
        [1.0, 1.0],
        60,
        5,
        {"m9": 0.7, "m2": 0.8},
    )
    assert fused == ["m1", "m4", "m2", "m9", "m3"]


def test_rrf_fuse_weight_zero_keeps_first_list_order():
    assert rrf_fuse([["a", "b", "c"], ["c", "z"]], [1.0, 0.0], 60, 3) == ["a", "b", "c"]


def test_rrf_fuse_counts_a_repeated_key_once():
    assert rrf_fuse([["a", "b", "a"]], [1.0], 60, 10) == ["a", "b"]


def test_keyword_sql_is_the_mcp_file_with_psycopg2_placeholders():
    raw = KEYWORD_SQL_PATH.read_text()
    sql = load_keyword_sql()
    assert "$" + "1" not in sql
    used = sorted({int(m) for m in re.findall(r"%\(p(\d+)\)s", sql)})
    assert used == list(range(1, 11))
    assert len(re.findall(r"\$\d+", raw)) == len(re.findall(r"%\(p\d+\)s", sql))


def test_hybrid_defaults_match_the_mcp():
    ts = HYBRID_TS.read_text()
    block = ts[ts.index("export const SEARCH_DEFAULTS") :]
    block = block[: block.index("}")]

    def ts_val(name: str) -> str:
        m = re.search(rf"\b{name}:\s*([^,\n]+)", block)
        assert m, name
        return m.group(1).strip()

    hp = HybridParams()
    assert float(ts_val("rrfK")) == hp.rrf_k
    assert float(ts_val("keywordWeight")) == hp.keyword_weight
    assert int(ts_val("candidates")) == hp.candidates
    assert float(ts_val("keywordMaxDf")) == hp.keyword_max_df
    assert float(ts_val("keywordRowBudget")) == hp.keyword_row_budget
    assert int(ts_val("keywordMaxTerms")) == hp.keyword_max_terms
    assert (ts_val("keywordIdentifiersOnly") == "true") == hp.keyword_identifiers_only


def test_percentile_nearest_rank():
    assert percentile([], 95) == 0.0
    assert percentile([5.0, 1.0, 3.0], 50) == 3.0
    assert percentile([float(i) for i in range(1, 101)], 95) == 95.0


def test_keyword_gold_items_are_tagged():
    items = load_gold(DEFAULT_GOLD)
    kw = [i for i in items if "keyword" in i.tags]
    assert len(kw) >= 10
    # A keyword item keeps an exact identifier / file name / error string.
    ident = re.compile(r"[A-Za-z0-9][_./:-][A-Za-z0-9]|^[A-Z0-9_]{4,}$")
    for it in kw:
        assert any(ident.search(tok.strip("?,.")) for tok in it.query.split()), it.id
