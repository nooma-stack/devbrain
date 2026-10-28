"""resummarize backend order, once-per-run fail-fast and per-run cap (no DB).

The DB-touching helpers are stubbed; every LLM path is a fake, so nothing
reaches codex or a hosted API.
"""

from __future__ import annotations

import cognify.extract as ex
import pytest
from cognify import backends, codex_exec, resummarize

USAGE0 = {"input_tokens": 0, "output_tokens": 0,
          "cache_read_tokens": 0, "cache_write_tokens": 0}


class _Calls:
    def __init__(self):
        self.codex = 0
        self.sonnet = 0


@pytest.fixture
def world(monkeypatch, tmp_path):
    """N waiting sessions, no DB, fake codex binary, recorders for writes."""
    state = {
        "targets": [(f"s{i:03d}", "p1") for i in range(50)],
        "tries": {},
        "replaced": [],
        "spend": [],
    }
    monkeypatch.setattr(
        resummarize, "discover_sessions_needing_resummarize",
        lambda conn, **kw: list(state["targets"]),
    )
    monkeypatch.setattr(resummarize, "_last_failed_tries", lambda conn, pid: dict(state["tries"]))
    monkeypatch.setattr(
        resummarize, "_load_session_raw_content",
        lambda conn, sid: "" if sid in state.get("empty", ()) else f"transcript {sid}",
    )
    monkeypatch.setattr(
        resummarize, "_replace_session_summary",
        lambda conn, **kw: state["replaced"].append((kw["session_id"], kw["source"])),
    )
    import observability.spend as spend

    monkeypatch.setattr(spend, "record_spend", lambda conn, **kw: state["spend"].append(kw))
    fake_bin = tmp_path / "codex"
    fake_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(ex, "_CODEX_BIN", str(fake_bin))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    return state


def _fake_codex(calls, result=("A codex summary.", None)):
    def run_json(prompt, schema, *, pass_name, required_keys, model=None):
        calls.codex += 1
        assert pass_name == "resummarize" and required_keys == ("summary",)
        assert "transcript" in prompt
        text, failure = result
        if failure:
            return None, failure, "fake"
        return {"summary": text}, None, None
    return run_json


def _fake_sonnet(calls, failure=None):
    def call(raw, model):
        calls.sonnet += 1
        if failure:
            return None, USAGE0, failure
        return "A sonnet summary.", {"input_tokens": 900, "output_tokens": 100,
                                     "cache_read_tokens": 0, "cache_write_tokens": 0}, None
    return call


def test_codex_first_with_per_run_cap(monkeypatch, world):
    calls = _Calls()
    monkeypatch.setattr(codex_exec, "run_json", _fake_codex(calls))
    monkeypatch.setattr(resummarize, "_call_sonnet", _fake_sonnet(calls))

    res = resummarize.run_resummarize(object(), max_sessions=None)

    assert res.sessions_discovered == 50
    assert res.sessions_processed == resummarize.MAX_SESSIONS_PER_RUN
    assert calls.codex == resummarize.MAX_SESSIONS_PER_RUN and calls.sonnet == 0
    assert {src for _, src in world["replaced"]} == {"codex"}
    # Oldest first.
    assert world["replaced"][0][0] == "s000"
    assert res.llm_calls == resummarize.MAX_SESSIONS_PER_RUN
    assert world["spend"] == []  # codex: no token spend
    assert res.blocked is None and res.failure_counts == {}


def test_no_backend_fails_fast_once(monkeypatch, world, tmp_path):
    """No codex login and no Anthropic credential: nothing is attempted and
    the run says so once, instead of 859 per-session no_auth failures."""
    monkeypatch.setenv("DEVBRAIN_CODEX_HOME", str(tmp_path / "revoked"))
    calls = _Calls()
    monkeypatch.setattr(codex_exec, "run_json", _fake_codex(calls))
    monkeypatch.setattr(resummarize, "_call_sonnet", _fake_sonnet(calls))

    res = resummarize.run_resummarize(object())

    assert calls.codex == 0 and calls.sonnet == 0
    assert res.blocked == "no_auth"
    assert res.sessions_attempted == 0 and res.sessions_failed == 0
    assert res.failure_counts == {}
    assert set(res.backend_status) == {"codex", "anthropic"}
    assert res.backend_status["codex"].startswith("no_auth")


def test_policy_refusal_is_labelled_refused(monkeypatch, world):
    """allowed_backends: [] (the PHI deployment): refused, nothing sent."""
    backends.reset(frozenset())
    calls = _Calls()
    monkeypatch.setattr(codex_exec, "run_json", _fake_codex(calls))
    monkeypatch.setattr(resummarize, "_call_sonnet", _fake_sonnet(calls))

    res = resummarize.run_resummarize(object())

    assert res.blocked == "refused"
    assert calls.codex == 0 and calls.sonnet == 0


def test_anthropic_fallback_when_codex_fails(monkeypatch, world):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-TEST")
    calls = _Calls()
    monkeypatch.setattr(codex_exec, "run_json", _fake_codex(calls, (None, "api")))
    monkeypatch.setattr(resummarize, "_call_sonnet", _fake_sonnet(calls))

    res = resummarize.run_resummarize(object(), max_sessions=2)

    assert res.sessions_processed == 2
    assert calls.codex == 2 and calls.sonnet == 2
    assert {src for _, src in world["replaced"]} == {"sonnet"}
    assert res.llm_calls == 4
    assert len(world["spend"]) == 2  # Anthropic tokens recorded


def test_codex_login_lost_mid_run_stops_without_credential(monkeypatch, world):
    calls = _Calls()
    monkeypatch.setattr(codex_exec, "run_json", _fake_codex(calls, (None, "no_auth")))
    monkeypatch.setattr(resummarize, "_call_sonnet", _fake_sonnet(calls))

    res = resummarize.run_resummarize(object())

    assert calls.codex == 1  # dropped after the first no_auth
    assert res.sessions_attempted == 1
    assert res.failure_counts == {"no_auth": 1}
    assert res.blocked == "no_auth"
    assert "failed mid-run" in res.backend_status["codex"]


def test_api_failure_streak_aborts(monkeypatch, world):
    calls = _Calls()
    monkeypatch.setattr(codex_exec, "run_json", _fake_codex(calls, (None, "api")))
    monkeypatch.setattr(resummarize, "_call_sonnet", _fake_sonnet(calls))

    res = resummarize.run_resummarize(object())

    assert calls.codex == resummarize.MAX_CONSECUTIVE_API_FAILURES
    assert res.aborted == "api_failures"
    assert res.failure_counts == {"api": resummarize.MAX_CONSECUTIVE_API_FAILURES}


def test_repeatedly_failing_sessions_go_to_the_back(monkeypatch, world):
    world["targets"] = [("bad1", "p1"), ("bad2", "p1"), ("ok1", "p1"), ("ok2", "p1")]
    world["tries"] = {"bad1": resummarize.MAX_SESSION_TRIES, "bad2": 1, "gone": 9}
    world["empty"] = {"bad2"}
    calls = _Calls()
    monkeypatch.setattr(codex_exec, "run_json", _fake_codex(calls))
    monkeypatch.setattr(resummarize, "_call_sonnet", _fake_sonnet(calls))

    res = resummarize.run_resummarize(object(), max_sessions=3)

    # bad1 (3 tries) waits behind everything else; bad2 is retried.
    assert [sid for sid, _ in world["replaced"]] == ["ok1", "ok2"]
    assert res.failed_tries == {"bad1": resummarize.MAX_SESSION_TRIES, "bad2": 2}


def test_explicit_codex_backend_never_falls_back(monkeypatch, world):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-TEST")
    backends.reset(None, selected="codex")
    calls = _Calls()
    monkeypatch.setattr(codex_exec, "run_json", _fake_codex(calls, (None, "api")))
    monkeypatch.setattr(resummarize, "_call_sonnet", _fake_sonnet(calls))

    resummarize.run_resummarize(object(), max_sessions=2)

    assert calls.sonnet == 0


def test_pass_metadata_reports_blocked(monkeypatch, world, tmp_path):
    from cognify.orchestrator import _ensure_registry, _PASS_REGISTRY

    _ensure_registry()
    monkeypatch.setenv("DEVBRAIN_CODEX_HOME", str(tmp_path / "revoked"))
    res = _PASS_REGISTRY["resummarize"]().run(object(), None)
    md = res.metadata
    assert md["blocked"] == "no_auth"
    assert md["sessions_discovered"] == 50 and md["backlog_remaining"] == 50
    assert md["sessions_processed"] == 0
    assert "allowed_backends" in md and "backend_status" in md
    assert res.rows_processed == 0 and res.llm_calls == 0


# ── codex_exec ──────────────────────────────────────────────────────────────


class _Proc:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = rc, stdout, stderr


def test_codex_exec_reads_output_file(monkeypatch, tmp_path):
    fake_bin = tmp_path / "codex"
    fake_bin.write_text("")
    monkeypatch.setattr(ex, "_CODEX_BIN", str(fake_bin))
    seen = {}

    def run(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        out = cmd[cmd.index("-o") + 1]
        with open(out, "w") as fh:
            fh.write('{"summary": "done"}')
        return _Proc()

    monkeypatch.setattr(codex_exec.subprocess, "run", run)
    parsed, failure, _ = codex_exec.run_json(
        "p", {"type": "object"}, pass_name="resummarize", required_keys=("summary",)
    )
    assert failure is None and parsed == {"summary": "done"}
    assert seen["cmd"][seen["cmd"].index("-m") + 1] == ex._CODEX_DEFAULT_MODEL
    assert seen["env"]["CODEX_HOME"]  # the isolated home, never ~/.codex


def test_codex_exec_salvages_and_labels(monkeypatch, tmp_path):
    fake_bin = tmp_path / "codex"
    fake_bin.write_text("")
    monkeypatch.setattr(ex, "_CODEX_BIN", str(fake_bin))
    monkeypatch.setattr(
        codex_exec.subprocess, "run",
        lambda cmd, **kw: _Proc(1, 'noise {"summary": "salvaged"} tokens used'),
    )
    parsed, failure, _ = codex_exec.run_json(
        "p", {}, pass_name="resummarize", required_keys=("summary",)
    )
    assert parsed == {"summary": "salvaged"} and failure is None

    monkeypatch.setattr(
        codex_exec.subprocess, "run",
        lambda cmd, **kw: _Proc(1, "", 'ERROR: {"error": {"message": "quota"}}'),
    )
    parsed, failure, detail = codex_exec.run_json(
        "p", {}, pass_name="resummarize", required_keys=("summary",)
    )
    assert parsed is None and failure == "api" and "quota" in detail


def test_codex_exec_refusals(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise AssertionError("codex must not run")

    monkeypatch.setattr(codex_exec.subprocess, "run", boom)
    backends.reset(frozenset({"anthropic"}))
    assert codex_exec.run_json("p", {}, pass_name="edges", required_keys=("x",))[1] == "refused"
    backends.reset()
    monkeypatch.setenv("DEVBRAIN_CODEX_HOME", str(tmp_path / "missing"))
    assert codex_exec.run_json("p", {}, pass_name="edges", required_keys=("x",))[1] == "no_auth"


# ── backends.chain / usable ─────────────────────────────────────────────────


def test_chain_orders(monkeypatch):
    assert backends.chain("codex", "claude-sonnet-5") == ["codex", "anthropic"]
    assert backends.chain("codex", "none") == ["codex"]
    assert backends.chain("claude-sonnet-5", "claude-sonnet-5") == ["anthropic"]
    backends.reset(None, selected="openai")
    assert backends.chain("codex", "claude-sonnet-5") == ["openai"]
    backends.reset(None, selected=backends.INVALID)
    assert backends.chain("codex", "claude-sonnet-5") == [backends.INVALID]


def test_usable_and_blocked_label(monkeypatch, tmp_path):
    fake_bin = tmp_path / "codex"
    fake_bin.write_text("")
    monkeypatch.setattr(ex, "_CODEX_BIN", str(fake_bin))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    ok, reasons = backends.usable(["codex", "anthropic"], "edges")
    assert ok == ["codex"] and reasons["anthropic"].startswith("no_auth")

    monkeypatch.setattr(ex, "_CODEX_BIN", str(tmp_path / "absent"))
    ok, reasons = backends.usable(["codex"], "edges")
    assert ok == [] and backends.blocked_label(reasons) == "unavailable"

    backends.reset(frozenset())
    ok, reasons = backends.usable(["codex", "anthropic", backends.INVALID], "edges")
    assert ok == [] and backends.blocked_label(reasons) == "refused"
    assert backends.blocked_label({"codex": "refused: x", "anthropic": "no_auth: y"}) == "no_auth"
