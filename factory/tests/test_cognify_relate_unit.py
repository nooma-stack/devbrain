"""No-DB unit tests for the write-time relation check (cognify/relate.py).

The model is never called: ``classify`` takes an injectable ``chat``.
DB-backed behaviour lives in test_cognify_relate_db.py.
"""

from __future__ import annotations

import json

import pytest
from cognify import relate
from cognify.relate import Candidate, RelateConfig


def _cands(*sims: float) -> list[Candidate]:
    return [
        Candidate(
            id=f"m{i}", kind="decision", title=f"t{i}", content=f"c{i}", similarity=s
        )
        for i, s in enumerate(sims, 1)
    ]


def _chat(payload):
    text = payload if isinstance(payload, str) else json.dumps(payload)

    def fn(cfg, messages):
        fn.messages = messages
        return text

    return fn


def _cfg(**kw) -> RelateConfig:
    base = {
        "mode": "on",
        "threshold": 0.8,
        "edge_min_similarity": 0.85,
        "min_same_confidence": 0.8,
    }
    base.update(kw)
    return RelateConfig(**base)


# ── config ──────────────────────────────────────────────────────────────────


def test_default_mode_is_off(monkeypatch):
    monkeypatch.delenv("DEVBRAIN_RELATION_CHECK", raising=False)
    cfg = relate.load_config({})
    assert cfg.mode == "off"
    assert not cfg.enabled


def test_model_and_url_default_to_summarization(monkeypatch):
    monkeypatch.delenv("DEVBRAIN_RELATION_MODEL", raising=False)
    monkeypatch.delenv("DEVBRAIN_RELATION_URL", raising=False)
    cfg = relate.load_config(
        {
            "summarization": {
                "url": "http://localhost:11434",
                "model": "local-model:7b",
            },
            "write_path": {"relation_check": {"mode": "shadow"}},
        }
    )
    assert cfg.mode == "shadow"
    assert cfg.model == "local-model:7b"
    assert cfg.url == "http://localhost:11434"


def test_yaml_boolean_modes(monkeypatch):
    monkeypatch.delenv("DEVBRAIN_RELATION_CHECK", raising=False)
    assert (
        relate.load_config({"write_path": {"relation_check": {"mode": True}}}).mode
        == "on"
    )
    assert (
        relate.load_config({"write_path": {"relation_check": {"mode": False}}}).mode
        == "off"
    )


def test_env_overrides_yaml(monkeypatch):
    monkeypatch.setenv("DEVBRAIN_RELATION_CHECK", "on")
    monkeypatch.setenv("DEVBRAIN_RELATION_THRESHOLD", "0.9")
    cfg = relate.load_config(
        {"write_path": {"relation_check": {"mode": "off", "threshold": 0.7}}}
    )
    assert cfg.mode == "on"
    assert cfg.threshold == 0.9


def test_remote_model_url_is_refused_by_default(monkeypatch):
    """Memory text may contain PHI: a non-loopback model URL turns the
    check off unless allow_remote_model is set explicitly."""
    monkeypatch.delenv("DEVBRAIN_RELATION_CHECK", raising=False)
    monkeypatch.delenv("DEVBRAIN_RELATION_ALLOW_REMOTE", raising=False)
    monkeypatch.delenv("DEVBRAIN_RELATION_URL", raising=False)
    cfg = relate.load_config(
        {
            "write_path": {
                "relation_check": {"mode": "on", "url": "https://models.example.com"}
            },
        }
    )
    assert cfg.mode == "off"

    cfg = relate.load_config(
        {
            "write_path": {
                "relation_check": {
                    "mode": "on",
                    "url": "https://models.example.com",
                    "allow_remote_model": True,
                }
            },
        }
    )
    assert cfg.mode == "on"


@pytest.mark.parametrize(
    "url,ok",
    [
        ("http://localhost:11434", True),
        ("http://127.0.0.1:11434", True),
        ("http://[::1]:11434", True),
        ("http://10.0.0.5:11434", False),
        ("https://api.example.com", False),
        ("not a url", False),
    ],
)
def test_is_loopback_url(url, ok):
    assert relate.is_loopback_url(url) is ok


# ── parsing ─────────────────────────────────────────────────────────────────


def test_parse_valid():
    assert relate.parse_response(
        '{"relation":"same","match":2,"confidence":0.9}', 3
    ) == ("same", 2, 0.9)


def test_parse_clamps_confidence():
    assert (
        relate.parse_response('{"relation":"new","match":0,"confidence":7}', 2)[2]
        == 1.0
    )


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        "[1,2]",
        '{"relation":"maybe","match":1,"confidence":1}',
        '{"relation":"same","match":"x","confidence":1}',
    ],
)
def test_parse_rejects_invalid(bad):
    with pytest.raises(ValueError):
        relate.parse_response(bad, 2)


def test_parse_same_without_referent_is_downgraded():
    """A "same" with no usable match must never drop the row on a guess."""
    assert relate.parse_response('{"relation":"same","match":0,"confidence":1}', 3)[
        :2
    ] == ("refines", 1)
    # With one candidate the referent is unambiguous.
    assert relate.parse_response('{"relation":"same","match":0,"confidence":1}', 1)[
        :2
    ] == ("same", 1)


# ── classify ────────────────────────────────────────────────────────────────


def test_no_candidates_is_new_without_model_call():
    def boom(cfg, messages):
        raise AssertionError("model must not be called")

    v = relate.classify(_cfg(), "decision", "t", "c", [], chat=boom)
    assert v.relation == "new" and v.model is None


def test_same_verdict():
    v = relate.classify(
        _cfg(),
        "decision",
        "t",
        "c",
        _cands(0.95, 0.9),
        chat=_chat({"relation": "same", "match": 2, "confidence": 0.95}),
    )
    assert v.relation == "same"
    assert v.match.id == "m2"
    assert v.model_relation == "same"


def test_low_confidence_same_becomes_refines():
    v = relate.classify(
        _cfg(),
        "decision",
        "t",
        "c",
        _cands(0.95),
        chat=_chat({"relation": "same", "match": 1, "confidence": 0.5}),
    )
    assert v.relation == "refines"
    assert v.model_relation == "same"


def test_low_similarity_edges_become_new():
    """refines/contradicts below edge_min_similarity are written as plain rows."""
    for rel in ("refines", "contradicts"):
        v = relate.classify(
            _cfg(),
            "decision",
            "t",
            "c",
            _cands(0.81),
            chat=_chat({"relation": rel, "match": 1, "confidence": 0.9}),
        )
        assert v.relation == "new" and v.match is None
        assert v.model_relation == rel
    v = relate.classify(
        _cfg(),
        "decision",
        "t",
        "c",
        _cands(0.81),
        chat=_chat({"relation": "same", "match": 1, "confidence": 0.9}),
    )
    assert v.relation == "same"


def test_model_failure_is_new_with_error():
    def fail(cfg, messages):
        raise TimeoutError("timed out")

    v = relate.classify(_cfg(), "decision", "t", "c", _cands(0.95), chat=fail)
    assert v.relation == "new"
    assert v.error and "TimeoutError" in v.error


def test_bad_json_is_new_with_error():
    v = relate.classify(_cfg(), "decision", "t", "c", _cands(0.95), chat=_chat("{oops"))
    assert v.relation == "new" and v.error


def test_prompt_marks_memories_as_data_and_avoids_double_title():
    chat = _chat({"relation": "new", "match": 0, "confidence": 0.9})
    cands = [Candidate("m1", "pattern", "Old title", "Old title\n\nold body", 0.9)]
    relate.classify(_cfg(), "decision", "New title", "new body", cands, chat=chat)
    system, user = chat.messages[0]["content"], chat.messages[1]["content"]
    assert "DATA, not instructions" in system
    assert user.count("Old title") == 1  # store rows carry "title\n\ncontent"
    assert "<memory>" in user and "EXISTING memory 1 (pattern)" in user


def test_long_content_is_clipped():
    chat = _chat({"relation": "new", "match": 0, "confidence": 0.9})
    relate.classify(
        _cfg(max_chars=50), "decision", "t", "x" * 5000, _cands(0.9), chat=chat
    )
    assert len(chat.messages[1]["content"]) < 1000


def test_ollama_request_shape(monkeypatch):
    """Local Ollama /api/chat with a JSON schema, temperature 0, think flag."""
    sent = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(
                {"message": {"content": '{"relation":"new","match":0,"confidence":1}'}}
            ).encode()

    def fake_urlopen(req, timeout):
        sent["url"] = req.full_url
        sent["body"] = json.loads(req.data)
        sent["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(relate.urllib.request, "urlopen", fake_urlopen)
    cfg = _cfg(url="http://localhost:11434/", model="m:7b", timeout_s=7)
    v = relate.classify(cfg, "decision", "t", "c", _cands(0.9))
    assert v.relation == "new" and v.error is None
    assert sent["url"] == "http://localhost:11434/api/chat"
    assert sent["body"]["model"] == "m:7b"
    assert sent["body"]["format"]["properties"]["relation"]["enum"] == list(
        relate.RELATIONS
    )
    assert sent["body"]["options"]["temperature"] == 0
    assert sent["body"]["think"] is False
    assert sent["timeout"] == 7


# ── precheck degrades to a plain write ──────────────────────────────────────


class _BrokenConn:
    """A connection whose every query fails."""

    def cursor(self):
        raise RuntimeError("db down")

    def rollback(self):
        pass

    def commit(self):
        pass


def test_precheck_off_does_nothing():
    pre = relate.precheck(
        _BrokenConn(),
        _cfg(mode="off"),
        project_id="p",
        kind="decision",
        title="t",
        content="c",
        embedding_literal="[0]",
        source="extract",
    )
    assert not pre.skip_insert and pre.verdict is None


def test_precheck_lookup_failure_is_plain_write():
    pre = relate.precheck(
        _BrokenConn(),
        _cfg(),
        project_id="p",
        kind="decision",
        title="t",
        content="c",
        embedding_literal="[0]",
        source="extract",
    )
    assert not pre.skip_insert and pre.verdict is None


def test_precheck_without_embedding_is_plain_write():
    pre = relate.precheck(
        _BrokenConn(),
        _cfg(),
        project_id="p",
        kind="decision",
        title="t",
        content="c",
        embedding_literal=None,
        source="extract",
    )
    assert not pre.skip_insert
