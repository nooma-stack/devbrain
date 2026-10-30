"""Fan-out under a rate limit: what the failure is called and when the run
stops. No DB: discovery, classification and the write are stubbed."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_FACTORY = Path(__file__).resolve().parents[1]
if str(_FACTORY) not in sys.path:
    sys.path.insert(0, str(_FACTORY))

from cognify import backends, entities, fanout  # noqa: E402
from cognify.fanout import ClassificationResult  # noqa: E402

RATE_LIMIT = "codex error (rc=1): exceeded retry limit, last status: 429 Too Many Requests"
CAPACITY = "codex error (rc=1): Selected model is at capacity. Please try a different model."
REVOKED = (
    "codex error (rc=1): Your access token could not be refreshed because your "
    "refresh token was revoked. Please log out and sign in again."
)


@pytest.mark.parametrize("detail", [RATE_LIMIT, CAPACITY, "You hit your usage limit"])
def test_rate_limits_get_their_own_label(detail):
    assert backends.failure_label("api", detail) == "api:rate_limited"


@pytest.mark.parametrize("failure,detail", [
    ("api", REVOKED),
    ("api", "codex exec timeout"),
    ("api", None),
    ("no_auth", RATE_LIMIT),
    ("json_parse", RATE_LIMIT),
])
def test_other_failures_keep_their_label(failure, detail):
    assert backends.failure_label(failure, detail) == failure


def _classify_with(monkeypatch, codex_detail, anthropic_failure="no_auth"):
    monkeypatch.setattr(fanout, "_load_active_taxonomy",
                        lambda conn: [{"slug": "p", "description": "d"}])
    monkeypatch.setattr(fanout, "_load_session_content", lambda conn, sid: "text")
    monkeypatch.setattr(fanout, "render_taxonomy", lambda t: "t")
    monkeypatch.setattr(fanout, "_codex_classify",
                        lambda *a, **k: (None, "api", codex_detail))
    monkeypatch.setattr(
        fanout, "_anthropic_classify",
        lambda *a, **k: (None, anthropic_failure, "no Anthropic credential", None),
    )
    monkeypatch.setattr(backends, "selected", lambda: None)
    return fanout.classify_session("s1", MagicMock(), model="codex")


def test_rate_limit_is_not_reported_as_a_missing_login(monkeypatch):
    result = _classify_with(monkeypatch, RATE_LIMIT)
    assert result.failure == "api"
    assert result.failure_detail == RATE_LIMIT


def test_revoked_login_still_reports_no_auth(monkeypatch):
    assert _classify_with(monkeypatch, REVOKED).failure == "no_auth"


def _run(monkeypatch, outcomes):
    """run_fanout over one session per outcome, returning (result, calls)."""
    calls = []
    recorded = []
    outcomes = list(outcomes)

    def classify(session_id, conn, **kw):
        calls.append(session_id)
        failure, detail = outcomes[len(calls) - 1]
        return ClassificationResult(
            session_id=session_id, failure=failure, failure_detail=detail,
        )

    monkeypatch.setattr(fanout, "discover_sessions_needing_fanout",
                        lambda conn, **kw: [f"s{i}" for i in range(len(outcomes))])
    monkeypatch.setattr(fanout, "classify_session", classify)
    monkeypatch.setattr(fanout, "_record_no_targets",
                        lambda conn, sid: recorded.append(sid))
    result = fanout.run_fanout(MagicMock())
    return result, calls, recorded


LIMITED = ("api", RATE_LIMIT)
EMPTY = ("empty", "no target")
TIMEOUT = ("api", "codex exec timeout")


def test_run_stops_after_three_rate_limits_in_a_row(monkeypatch):
    result, calls, _ = _run(monkeypatch, [LIMITED] * 10)
    assert len(calls) == fanout.MAX_CONSECUTIVE_BLOCKED
    assert result.stopped == "api:rate_limited"
    assert result.failure_counts == {"api:rate_limited": 3}


def test_run_stops_when_no_backend_is_left(monkeypatch):
    result, calls, _ = _run(monkeypatch, [("no_auth", "no credential")] * 10)
    assert len(calls) == fanout.MAX_CONSECUTIVE_BLOCKED
    assert result.stopped == "no_auth"


def test_a_good_session_resets_the_count(monkeypatch):
    outcomes = [LIMITED, LIMITED, EMPTY, LIMITED, LIMITED, EMPTY]
    result, calls, _ = _run(monkeypatch, outcomes)
    assert len(calls) == len(outcomes)
    assert result.stopped is None


def test_ordinary_api_errors_do_not_stop_the_run(monkeypatch):
    """One session timing out says nothing about the next one."""
    result, calls, _ = _run(monkeypatch, [TIMEOUT] * 5)
    assert len(calls) == 5
    assert result.stopped is None
    assert result.failure_counts == {"api": 5}


def test_only_empty_sessions_are_remembered(monkeypatch):
    _, _, recorded = _run(monkeypatch, [EMPTY, TIMEOUT, EMPTY, LIMITED])
    assert recorded == ["s0", "s2"]


def test_entities_keeps_the_rate_limit_when_there_is_no_fallback(monkeypatch):
    res = entities.LLMResult(failure="api", detail=RATE_LIMIT)
    monkeypatch.setattr(entities, "_codex_call", lambda *a: res)
    monkeypatch.setattr(entities, "_fallback_configured", lambda: True)
    monkeypatch.setattr(entities, "_anthropic_call",
                        lambda *a: entities.LLMResult(failure="no_auth", detail="x"))
    monkeypatch.setattr(backends, "selected", lambda: None)
    assert entities.call_llm("s", "u").failure == "api"


def test_entities_revoked_login_still_reports_no_auth(monkeypatch):
    monkeypatch.setattr(entities, "_codex_call",
                        lambda *a: entities.LLMResult(failure="api", detail=REVOKED))
    monkeypatch.setattr(entities, "_fallback_configured", lambda: True)
    monkeypatch.setattr(entities, "_anthropic_call",
                        lambda *a: entities.LLMResult(failure="no_auth", detail="x"))
    monkeypatch.setattr(backends, "selected", lambda: None)
    assert entities.call_llm("s", "u").failure == "no_auth"
