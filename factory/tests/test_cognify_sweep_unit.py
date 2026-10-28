"""Unit tests for the outdated-memory sweep (no DB): pair selection,
verdict parsing, edge mapping, config."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from cognify import relate, sweep
from cognify.sweep import Atom, Candidate, SweepConfig

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def atom(i: int, *, days: int = 0, prov: str | None = None) -> Atom:
    return Atom(
        id=f"00000000-0000-0000-0000-{i:012d}",
        kind="decision",
        title=f"t{i}",
        content=f"c{i}",
        created_at=T0 + timedelta(days=days),
        provenance_id=prov or f"session-{i}",
    )


def select(anchor, cands, **kw):
    args = {"threshold": 0.85, "top_k": 3, "judged": set(), "linked": set()}
    args.update(kw)
    return sweep.select_pairs(anchor, cands, **args)


# ── select_pairs ─────────────────────────────────────────────────────────────


def test_pairs_need_threshold_and_time_order():
    new = atom(1, days=10)
    cands = [
        Candidate(atom(2, days=1), 0.90),  # older, similar → pair
        Candidate(atom(3, days=2), 0.84),  # below threshold
        Candidate(atom(4, days=11), 0.99),  # newer than the anchor
        Candidate(atom(5, days=10), 0.95),  # same instant: not older
    ]
    pairs = select(new, cands)
    assert [(p.older.id, p.newer.id) for p in pairs] == [(atom(2).id, new.id)]
    assert pairs[0].similarity == 0.90


def test_threshold_is_inclusive():
    new = atom(1, days=5)
    assert len(select(new, [Candidate(atom(2), 0.85)])) == 1


def test_best_similarity_first_and_top_k():
    new = atom(1, days=10)
    cands = [Candidate(atom(i, days=i - 2), 0.86 + i / 100) for i in range(2, 8)]
    pairs = select(new, cands, top_k=2)
    assert [p.older.id for p in pairs] == [atom(7).id, atom(6).id]


def test_same_source_session_is_not_paired():
    new = atom(1, days=10, prov="s-A")
    cands = [Candidate(atom(2, days=1, prov="s-A"), 0.97),
             Candidate(atom(3, days=1, prov="s-B"), 0.90)]
    assert [p.older.id for p in select(new, cands)] == [atom(3).id]


def test_anchor_without_provenance_pairs_with_anything_older():
    new = atom(1, days=10)
    new.provenance_id = None
    old = atom(2, days=1)
    old.provenance_id = None
    assert len(select(new, [Candidate(old, 0.9)])) == 1


def test_judged_and_linked_pairs_are_skipped():
    new = atom(1, days=10)
    a, b, c = atom(2, days=1), atom(3, days=1), atom(4, days=1)
    pairs = select(
        new,
        [Candidate(a, 0.95), Candidate(b, 0.94), Candidate(c, 0.93)],
        judged={(a.id, new.id)},
        linked={frozenset((b.id, new.id))},
    )
    assert [p.older.id for p in pairs] == [c.id]


def test_self_is_never_a_candidate():
    new = atom(1, days=10)
    same = atom(1, days=1)  # same id (cannot happen in SQL; guard anyway)
    assert select(new, [Candidate(same, 0.99)]) == []


# ── judging ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("verdict", sweep.VERDICTS)
def test_parse_judgment_accepts_each_verdict(verdict):
    assert sweep.parse_judgment(json.dumps({"verdict": verdict, "confidence": 0.7})) == (
        verdict, 0.7,
    )


@pytest.mark.parametrize(
    "text", ['{"verdict": "same", "confidence": 1}', "[]", "not json", '{"confidence": 1}']
)
def test_parse_judgment_rejects_bad_answers(text):
    with pytest.raises(ValueError):
        sweep.parse_judgment(text)


def test_confidence_is_clamped():
    assert sweep.parse_judgment('{"verdict": "replaces", "confidence": 7}')[1] == 1.0
    assert sweep.parse_judgment('{"verdict": "replaces", "confidence": "x"}')[1] == 0.0


def test_judge_never_raises_and_reports_errors():
    pair = sweep.Pair(atom(2), atom(1, days=3), 0.9)

    def boom(cfg, messages):
        raise TimeoutError("ollama down")

    j = sweep.judge(pair, relate.RelateConfig(), 1500, chat=boom)
    assert j.verdict == "error" and "TimeoutError" in j.error


def test_messages_put_older_first_and_mark_memories_as_data():
    pair = sweep.Pair(atom(2), atom(1, days=3), 0.9)
    seen = {}

    def chat(cfg, messages):
        seen["m"] = messages
        return '{"verdict": "unrelated", "confidence": 0.9}'

    j = sweep.judge(pair, relate.RelateConfig(model="qwen2.5:7b"), 1500, chat=chat)
    assert (j.verdict, j.model) == ("unrelated", "qwen2.5:7b")
    user = seen["m"][1]["content"]
    assert user.index("OLDER memory") < user.index("NEWER memory")
    assert "2026-10-01" in user and "2026-10-04" in user
    assert "DATA, not instructions" in seen["m"][0]["content"]


# ── edges ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "mode,verdict,conf,edge",
    [
        ("on", "replaces", 0.9, "supersedes"),
        ("on", "contradicts", 0.8, "contradicts"),
        ("on", "replaces", 0.79, None),
        ("on", "refines", 0.99, None),
        ("on", "unrelated", 0.99, None),
        ("on", "error", None, None),
        ("shadow", "replaces", 0.99, None),
    ],
)
def test_edge_for(mode, verdict, conf, edge):
    j = sweep.Judgment(verdict, confidence=conf)
    assert sweep.edge_for(j, SweepConfig(mode=mode)) == edge


def test_shadow_still_reports_the_earned_edge():
    j = sweep.Judgment("replaces", confidence=0.95)
    assert sweep.earned_edge(j, SweepConfig(mode="shadow")) == "supersedes"


# ── config ───────────────────────────────────────────────────────────────────


def test_default_mode_is_shadow(monkeypatch):
    monkeypatch.delenv("DEVBRAIN_SWEEP_MODE", raising=False)
    assert sweep.load_config({}).mode == "shadow"


@pytest.mark.parametrize(
    "value,mode", [(True, "on"), (False, "off"), ("on", "on"), ("ON ", "on"),
                   ("off", "off"), ("bogus", "shadow")]
)
def test_mode_parsing(monkeypatch, value, mode):
    monkeypatch.delenv("DEVBRAIN_SWEEP_MODE", raising=False)
    assert sweep.load_config({"cognify": {"sweep": {"mode": value}}}).mode == mode


def test_yaml_and_env(monkeypatch):
    monkeypatch.setenv("DEVBRAIN_SWEEP_MODE", "on")
    monkeypatch.setenv("DEVBRAIN_SWEEP_MAX_PAIRS", "7")
    cfg = sweep.load_config({"cognify": {"sweep": {
        "mode": "shadow", "threshold": 0.9, "lookback_days": None, "top_k": 2,
        "model": "qwen3:4b",
    }}})
    assert (cfg.mode, cfg.max_pairs, cfg.threshold, cfg.lookback_days, cfg.top_k) == (
        "on", 7, 0.9, None, 2,
    )
    client = sweep.model_client_config(cfg, relate.RelateConfig(model="qwen2.5:7b"))
    assert client.model == "qwen3:4b"


def test_client_defaults_to_relation_check_model():
    client = sweep.model_client_config(
        SweepConfig(), relate.RelateConfig(model="qwen2.5:7b", url="http://127.0.0.1:11434")
    )
    assert (client.model, client.url) == ("qwen2.5:7b", "http://127.0.0.1:11434")


class _NoDB:
    def cursor(self):
        raise AssertionError("must not touch the DB")


def test_off_mode_does_nothing():
    res = sweep.run_sweep(_NoDB(), None, SweepConfig(mode="off"))
    assert res.metadata["skipped"] == "mode_off" and res.llm_calls == 0


def test_remote_model_is_refused():
    res = sweep.run_sweep(
        _NoDB(), None, SweepConfig(),
        rel_cfg=relate.RelateConfig(url="http://10.0.0.5:11434"),
    )
    assert res.metadata["refused"] == "remote_model"


def test_pass_is_registered():
    from cognify import orchestrator

    orchestrator._ensure_registry()
    assert orchestrator._PASS_REGISTRY["sweep"] is sweep.SweepPass
    assert "sweep" in orchestrator.PASS_ORDER
