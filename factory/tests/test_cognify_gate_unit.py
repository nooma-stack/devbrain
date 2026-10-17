"""No-DB unit tests for the extract gate's config and decision rules."""

from __future__ import annotations

import pytest
from cognify import gate
from cognify.gate import GateConfig, UnitFeatures


def _f(**kw) -> UnitFeatures:
    base = {"unit_id": "u1", "shape": "raw", "source_rows": 5, "source_chars": 5000}
    base.update(kw)
    return UnitFeatures(**base)


def test_default_mode_is_off(monkeypatch):
    monkeypatch.delenv("DEVBRAIN_EXTRACT_GATE", raising=False)
    cfg = gate.load_config({})
    assert cfg.mode == "off" and not cfg.enabled


@pytest.mark.parametrize(
    "raw,mode",
    [
        ("dry_run", "dry_run"),
        ("dry-run", "dry_run"),
        ("ON", "on"),
        (True, "on"),
        (False, "off"),
        (None, "off"),
        ("bogus", "off"),
    ],
)
def test_mode_parsing(monkeypatch, raw, mode):
    monkeypatch.delenv("DEVBRAIN_EXTRACT_GATE", raising=False)
    assert gate.load_config({"cognify": {"extract_gate": {"mode": raw}}}).mode == mode


def test_env_overrides_yaml(monkeypatch):
    monkeypatch.setenv("DEVBRAIN_EXTRACT_GATE", "dry_run")
    cfg = gate.load_config(
        {"cognify": {"extract_gate": {"mode": "on", "min_chars": 50}}}
    )
    assert cfg.mode == "dry_run" and cfg.min_chars == 50


def test_yaml_thresholds(monkeypatch):
    monkeypatch.delenv("DEVBRAIN_EXTRACT_GATE", raising=False)
    cfg = gate.load_config(
        {
            "cognify": {
                "extract_gate": {
                    "skip_path_parts": ["/bots/"],
                    "skip_clis": [],
                    "skip_novel_share": 0.0,
                    "max_tool_share": 0.5,
                    "novelty": False,
                }
            }
        }
    )
    assert cfg.skip_path_parts == ["/bots/"] and cfg.skip_clis == []
    assert cfg.skip_novel_share == 0.0 and cfg.max_tool_share == 0.5 and not cfg.novelty


def test_extract_when_nothing_fires():
    d = gate.decide(_f(human_turns=3, tool_share=0.2, novel_share=0.9), GateConfig())
    assert d.action == "extract" and d.reasons == []


@pytest.mark.parametrize(
    "feats,reason",
    [
        ({"source_rows": 0, "source_chars": 0, "shape": "empty"}, "no_source"),
        ({"shape": "atom"}, "atom_source"),
        ({"shape": "summary", "cli": "closure-backfill"}, "cli:closure-backfill"),
        ({"source_path": "/x/proj/abc/subagents/agent-1.jsonl"}, "path"),
        ({"source_path": "/x/-Users-me-support-runner/s.jsonl"}, "path"),
        ({"source_chars": 120}, "too_short"),
        ({"human_turns": 0}, "no_human_turns"),
    ],
)
def test_skip_rules(feats, reason):
    d = gate.decide(_f(**feats), GateConfig())
    assert d.action == "skip" and reason in d.reasons


def test_live_end_session_summary_is_extracted():
    d = gate.decide(_f(shape="summary", cli=None, source_chars=900), GateConfig())
    assert d.action == "extract"


def test_human_turn_rule_only_for_raw_transcripts():
    d = gate.decide(_f(shape="summary", human_turns=0), GateConfig())
    assert d.action == "extract"


def test_unknown_raw_features_do_not_skip():
    """Old non-JSON transcripts have no turn counts: no verdict from them."""
    d = gate.decide(_f(human_turns=None, tool_share=None), GateConfig())
    assert d.action == "extract"


def test_downgrade_rules():
    assert gate.decide(_f(human_turns=2, tool_share=0.95), GateConfig()).reasons == [
        "tool_heavy"
    ]
    d = gate.decide(_f(human_turns=2, novel_share=0.1), GateConfig())
    assert d.action == "downgrade" and d.reasons == ["low_novelty"]


def test_novelty_never_skips_by_default():
    d = gate.decide(_f(human_turns=2, novel_share=0.0), GateConfig())
    assert d.action == "downgrade"
    d = gate.decide(
        _f(human_turns=2, novel_share=0.0), GateConfig(skip_novel_share=0.0)
    )
    assert d.action == "skip" and "not_novel" in d.reasons


def test_skip_wins_over_downgrade_and_keeps_all_reasons():
    d = gate.decide(_f(human_turns=0, tool_share=0.99), GateConfig())
    assert d.action == "skip" and d.reasons == ["no_human_turns", "tool_heavy"]


def test_gate_log_summary_has_ids_and_reasons_only():
    log = gate.GateLog("dry_run")
    for i in range(60):
        log.add(gate.decide(_f(unit_id=f"u{i}", shape="atom"), GateConfig()))
    log.add(gate.decide(_f(unit_id="ok", human_turns=1), GateConfig()))
    s = log.summary()
    assert s["counts"] == {"extract": 1, "downgrade": 0, "skip": 60}
    assert s["reasons"] == {"atom_source": 60}
    assert len(s["ids"]["skip"]) == gate.GateLog.MAX_IDS
    assert set(s) == {"mode", "counts", "reasons", "ids"}
