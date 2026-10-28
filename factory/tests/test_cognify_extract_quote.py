"""Quote-checked extraction and content-derived identity (no DB).

Every extracted item must carry a quote that occurs word for word in the
session text the model saw (whitespace normalized only); items without one
are dropped and counted. Content keys are uuid5 over project + kind +
normalized title. Zero-yield runs must say why in the run-log metadata.
"""

from __future__ import annotations

import uuid

import cognify.extract as ex
import pytest
from cognify import backends

SOURCE = (
    "## Session\n"
    "We decided to keep the advisory lock per (pass, project).\n\n"
    "The   retry   loop backs off\nexponentially on 503s."
)


# ── verify_quote ─────────────────────────────────────────────────────────────


@pytest.fixture
def src():
    return ex.normalize_ws(SOURCE)


def test_exact_quote_passes(src):
    assert ex.verify_quote("keep the advisory lock per (pass, project).", src) is None


def test_whitespace_is_normalized_on_both_sides(src):
    # Source has runs of spaces and a newline; the quote has single spaces.
    assert ex.verify_quote("The retry loop backs off exponentially on 503s.", src) is None
    assert ex.verify_quote("The retry\n\tloop   backs off exponentially", src) is None


def test_case_and_punctuation_must_match(src):
    assert ex.verify_quote("we decided to keep the advisory lock", src) == "quote_not_found"
    assert ex.verify_quote("keep the advisory lock per pass, project", src) == "quote_not_found"


def test_paraphrase_and_ellipsis_fail(src):
    assert ex.verify_quote("We chose to keep an advisory lock", src) == "quote_not_found"
    assert ex.verify_quote("We decided ... per (pass, project).", src) == "quote_not_found"


def test_added_quotation_marks_fail(src):
    assert ex.verify_quote('"We decided to keep the advisory lock"', src) == "quote_not_found"


@pytest.mark.parametrize("quote", [None, "", "   ", 42, ["a"], "the retry"])
def test_missing_or_short_quote_is_no_quote(src, quote):
    assert ex.verify_quote(quote, src) == "no_quote"


# ── content_key ─────────────────────────────────────────────────────────────


def test_content_key_is_stable_and_normalized():
    p = uuid.uuid4()
    a = ex.content_key(p, "pattern", "Use UUID5 ids!", "x")
    b = ex.content_key(p, "pattern", "  use uuid5   IDs ", "something else")
    assert a == b
    assert isinstance(a, uuid.UUID) and a.version == 5


def test_content_key_separates_project_and_kind():
    p, q = uuid.uuid4(), uuid.uuid4()
    k = ex.content_key(p, "pattern", "Same title", "")
    assert k != ex.content_key(q, "pattern", "Same title", "")
    assert k != ex.content_key(p, "decision", "Same title", "")
    assert k != ex.content_key(p, "pattern", "Other title", "")


def test_content_key_falls_back_to_content_without_title():
    p = uuid.uuid4()
    assert ex.content_key(p, "decision", "", "Body A") == ex.content_key(
        p, "decision", "", "body  a"
    )
    assert ex.content_key(p, "decision", "", "Body A") != ex.content_key(
        p, "decision", "", "Body B"
    )


def test_schema_requires_quote():
    for key in ("lessons", "decisions"):
        item = ex._CODEX_OUTPUT_SCHEMA["properties"][key]["items"]
        assert "quote" in item["required"]
        assert item["properties"]["quote"] == {"type": "string"}


# ── extract_from_session with a stubbed backend ─────────────────────────────


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(
        ex, "_load_session_chunks",
        lambda c, s, p: [{"title": "Session", "content": SOURCE.split("\n", 1)[1]}],
    )
    written: list[dict] = []

    def fake_upsert(conn, **kw):
        written.append(kw)
        kw["report"]["outcome"] = "created"
        return f"m{len(written)}", True

    monkeypatch.setattr(ex, "_upsert_memory", fake_upsert)
    monkeypatch.setattr(ex, "record_spend", lambda conn, **kw: None)
    backends.reset(frozenset({"anthropic"}), selected="anthropic")
    return written


def _stub_llm(monkeypatch, payload):
    monkeypatch.setattr(ex, "_llm_extract", lambda *a, **k: dict(payload))


def test_items_without_valid_quote_are_dropped_and_counted(monkeypatch, session):
    _stub_llm(monkeypatch, {
        "lessons": [
            {"title": "Lock", "content": "c",
             "quote": "keep the advisory lock per (pass, project)."},
            {"title": "No quote", "content": "c"},
            {"title": "Invented", "content": "c", "quote": "We rewrote it all in Rust."},
        ],
        "decisions": [
            {"title": "Backoff", "content": "c",
             "quote": "The retry loop backs off exponentially on 503s."},
            "not an object",
        ],
        "_usage": {},
    })
    res = ex.extract_from_session(object(), "s1", "p1")
    assert (res.lessons_created, res.decisions_created) == (1, 1)
    assert res.items_returned == 5
    assert res.item_outcomes == {
        "created": 2, "no_quote": 1, "quote_not_found": 1, "empty_item": 1,
    }
    assert [w["title"] for w in session] == ["Lock", "Backoff"]
    # The normalized quote is what gets stored.
    assert session[1]["quote"] == "The retry loop backs off exponentially on 503s."
    assert res.zero_yield_reason is None


def test_all_dropped_is_a_zero_yield_reason(monkeypatch, session):
    _stub_llm(monkeypatch, {
        "lessons": [{"title": "Invented", "content": "c", "quote": "nothing like this"}],
        "decisions": [],
        "_usage": {},
    })
    res = ex.extract_from_session(object(), "s1", "p1")
    assert res.lessons_created == 0 and res.failure is None
    assert res.zero_yield_reason == "all_dropped"
    assert session == []


def test_quote_must_be_in_the_text_the_model_saw(monkeypatch, session):
    """Text past MAX_PROMPT_CHARS never reached the model, so it cannot be
    quoted."""
    long_tail = "x" * ex.MAX_PROMPT_CHARS + " The hidden tail sentence is here."
    monkeypatch.setattr(
        ex, "_load_session_chunks", lambda c, s, p: [{"title": "", "content": long_tail}]
    )
    _stub_llm(monkeypatch, {
        "lessons": [{"title": "T", "content": "c",
                     "quote": "The hidden tail sentence is here."}],
        "decisions": [], "_usage": {},
    })
    res = ex.extract_from_session(object(), "s1", "p1")
    assert res.item_outcomes == {"quote_not_found": 1}


def test_all_known_when_every_kept_item_exists(monkeypatch, session):
    def dup(conn, **kw):
        kw["report"]["outcome"] = "merged"
        return "old", False

    monkeypatch.setattr(ex, "_upsert_memory", dup)
    _stub_llm(monkeypatch, {
        "lessons": [{"title": "Lock", "content": "c",
                     "quote": "keep the advisory lock per (pass, project)."}],
        "decisions": [], "_usage": {},
    })
    res = ex.extract_from_session(object(), "s1", "p1")
    assert res.item_outcomes == {"merged": 1}
    assert res.zero_yield_reason == "all_known"


# ── the 2026-10-28 zero-yield bug: fallback without a credential ─────────────


def test_llm_extract_without_credential_is_refused_not_empty(monkeypatch):
    backends.reset(frozenset({"anthropic"}))
    monkeypatch.setattr(ex, "_resolve_auth", lambda: None)
    res = ex._llm_extract("text", model="claude-x")
    assert res["_failure"] == "refused"
    assert res["lessons"] == [] and res["decisions"] == []


def test_codex_failure_with_uncredentialed_fallback_reports_api(monkeypatch, session):
    """codex fails, the Anthropic fallback has no credential. Before the fix
    this was a "successful" call with nothing in it (failure None, 0 rows):
    the unit was marked done and lost. Now it stays an api failure (retried)
    and the fallback's refusal is visible."""
    backends.reset(frozenset({"codex", "anthropic"}))
    monkeypatch.setattr(ex, "_FALLBACK_MODEL", "claude-sonnet-5")
    monkeypatch.setattr(ex, "_codex_extract", lambda c, model=None: {
        "lessons": [], "decisions": [], "_usage": {}, "_failure": "api",
        "_failure_detail": "codex error: please sign in again"})
    monkeypatch.setattr(ex, "_resolve_auth", lambda: None)
    res = ex.extract_from_session(object(), "s1", "p1", model="codex")
    assert res.failure == "api"
    assert res.fallback == "failed:refused"
    assert res.zero_yield_reason == "api"


def test_empty_fallback_is_accepted(monkeypatch, session):
    """A fallback call that completed with nothing to extract is a result,
    not a codex failure to retry."""
    backends.reset(frozenset({"codex", "anthropic"}))
    monkeypatch.setattr(ex, "_FALLBACK_MODEL", "claude-sonnet-5")
    monkeypatch.setattr(ex, "_codex_extract", lambda c, model=None: {
        "lessons": [], "decisions": [], "_usage": {}, "_failure": "api"})
    monkeypatch.setattr(ex, "_llm_extract", lambda *a, **k: {
        "lessons": [], "decisions": [], "_usage": {}, "_failure": "empty"})
    res = ex.extract_from_session(object(), "s1", "p1", model="codex")
    assert res.failure == "empty"
    assert res.fallback == "used"


# ── pass metadata ────────────────────────────────────────────────────────────


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return None

    def fetchall(self):
        return self.rows


class _Conn:
    """Answers the pass's watermark/backlog/candidate queries."""

    def __init__(self, sessions):
        self.sessions = sessions

    def cursor(self):
        return _Cur([(s,) for s in self.sessions])


def test_pass_metadata_explains_zero_yield(monkeypatch):
    backends.reset(frozenset({"anthropic"}), selected="anthropic")
    results = {
        "a": ex.ExtractResult("a", llm_calls=1, failure="api"),
        "b": ex.ExtractResult(
            "b", llm_calls=1, items_returned=2,
            item_outcomes={"no_quote": 1, "quote_not_found": 1},
        ),
        "c": ex.ExtractResult(
            "c", llm_calls=1, lessons_created=1, items_returned=1,
            item_outcomes={"created": 1},
        ),
        "d": ex.ExtractResult("d", llm_calls=1, fallback="failed:refused",
                              failure="api"),
    }
    monkeypatch.setattr(
        ex, "extract_from_session",
        lambda conn, s, p, **kw: results[s],
    )
    from cognify import gate

    gate.reset_config(gate.GateConfig(mode="off"))
    try:
        res = ex.ExtractPass().run(_Conn(["a", "b", "c", "d"]), "p1")
    finally:
        gate.reset_config(None)
    md = res.metadata
    assert md["failure_counts"] == {"api": 2, "no_quote": 1, "quote_not_found": 1}
    assert md["zero_yield"] == {"api": 2, "all_dropped": 1}
    assert [z["id"] for z in md["zero_yield_ids"]] == ["a", "b", "d"]
    assert md["items"] == {"returned": 3, "no_quote": 1, "quote_not_found": 1,
                           "created": 1}
    assert md["fallback"] == {"failed:refused": 1}
    assert md["stopped"] is None


def test_pass_stops_at_first_refusal(monkeypatch):
    backends.reset(frozenset({"anthropic"}), selected="anthropic")
    calls = []

    def fake(conn, s, p, **kw):
        calls.append(s)
        return ex.ExtractResult(s, failure="refused")

    monkeypatch.setattr(ex, "extract_from_session", fake)
    from cognify import gate

    gate.reset_config(gate.GateConfig(mode="off"))
    try:
        res = ex.ExtractPass().run(_Conn(["a", "b", "c"]), "p1")
    finally:
        gate.reset_config(None)
    assert calls == ["a"]
    assert res.metadata["stopped"] == "refused"
    assert [b["id"] for b in res.metadata["backlog"]] == ["a", "b", "c"]
