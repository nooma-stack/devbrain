"""Codex backend failure handling and the run-to-run extract backlog.

No-DB tests first (codex output parsing, backlog bookkeeping); the DB test
at the bottom runs two real extract passes through the orchestrator.
"""

from __future__ import annotations

import json
import subprocess
import uuid

import cognify.extract as ex
import pytest

ERR_LINE = (
    'ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error",'
    '"message":"The \'gpt-old\' model is not supported when using Codex with a '
    'ChatGPT account."}}'
)


# ── salvage / error parsing ─────────────────────────────────────────────────


def test_salvage_ignores_codex_error_objects():
    """Regression: the error JSON codex prints was taken as the result and
    read as zero lessons ("empty"), so the fallback never ran."""
    assert ex._salvage_last_json("", "progress\n" + ERR_LINE + "\n" + ERR_LINE) is None


def test_salvage_still_recovers_a_real_result():
    good = json.dumps({"lessons": [{"title": "t", "content": "c"}], "decisions": []})
    got = ex._salvage_last_json(good + "\ntokens used: 10", ERR_LINE)
    assert got["lessons"][0]["title"] == "t"


def test_salvage_required_keys_for_fanout():
    payload = json.dumps({"per_project": []})
    assert ex._salvage_last_json(payload) is None
    assert ex._salvage_last_json(payload, required_keys=("per_project",)) == {
        "per_project": []
    }


def test_codex_error_message_and_model_hint(tmp_path, monkeypatch):
    (tmp_path / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {"slug": "gpt-new", "visibility": "list"},
                    {"slug": "hidden-one", "visibility": "hide"},
                ]
            }
        )
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    msg = ex.codex_error("", "noise\n" + ERR_LINE)
    assert "'gpt-old' model is not supported" in msg
    assert (
        "DEVBRAIN_CODEX_MODEL" in msg and "gpt-new" in msg and "hidden-one" not in msg
    )


def test_codex_error_none_without_error_lines():
    assert ex.codex_error("all good", "warning: something") is None


def test_codex_config_args(monkeypatch):
    monkeypatch.setattr(ex, "_CODEX_REASONING_EFFORT", "medium")
    assert ex.codex_config_args() == ["-c", 'model_reasoning_effort="medium"']
    for v in ("", "inherit", "INHERIT"):
        monkeypatch.setattr(ex, "_CODEX_REASONING_EFFORT", v)
        assert ex.codex_config_args() == []
    monkeypatch.setattr(ex, "_CODEX_REASONING_EFFORT", 'x"; rm')
    assert ex.codex_config_args() == []


class _Proc:
    def __init__(self, rc, out, err):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_rejected_model_is_an_api_failure(monkeypatch, tmp_path):
    """codex exits 1 with an API error and no -o file: the extractor must
    report "api" (which triggers the Anthropic fallback), not "empty"."""
    fake_bin = tmp_path / "codex"
    fake_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(ex, "_CODEX_BIN", str(fake_bin))
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _Proc(1, "", "user\nprompt\n" + ERR_LINE + "\n")

    monkeypatch.setattr(ex.subprocess, "run", fake_run)
    monkeypatch.setattr(ex, "_CODEX_REASONING_EFFORT", "low")
    res = ex._codex_extract("session text")
    assert res["_failure"] == "api"
    assert "not supported" in res["_failure_detail"]
    assert "-m" in seen["cmd"] and 'model_reasoning_effort="low"' in seen["cmd"]


def test_rejected_model_triggers_fallback(monkeypatch):
    monkeypatch.setattr(
        ex, "_load_session_chunks", lambda c, s, p: [{"title": "t", "content": "body"}]
    )
    monkeypatch.setattr(
        ex,
        "_codex_extract",
        lambda content, model=None: {
            "lessons": [],
            "decisions": [],
            "_usage": {},
            "_failure": "api",
            "_failure_detail": "codex error: model is not supported",
        },
    )
    called = {}

    def fake_llm(content, *, max_llm_calls=1, model=None):
        called["model"] = model
        return {"lessons": [], "decisions": [], "_usage": {}}

    monkeypatch.setattr(ex, "_llm_extract", fake_llm)
    monkeypatch.setattr(ex, "_FALLBACK_MODEL", "claude-test")
    res = ex.extract_from_session(object(), "s1", "p1", model="codex")
    assert called["model"] == "claude-test"
    assert res.failure is None


def test_timeout_is_api_failure(monkeypatch, tmp_path):
    fake_bin = tmp_path / "codex"
    fake_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(ex, "_CODEX_BIN", str(fake_bin))

    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(ex.subprocess, "run", boom)
    assert ex._codex_extract("x")["_failure"] == "api"


# ── backlog bookkeeping ─────────────────────────────────────────────────────


def test_next_backlog_carries_unreached_and_retries_api_failures():
    queue = ["a", "b", "c", "d", "e"]
    out = ex._next_backlog(
        queue, done={"a", "b"}, gate_skipped={"c"}, retry=["b"], tries={"b": 0, "d": 1}
    )
    assert out == [
        {"id": "d", "tries": 1},
        {"id": "e", "tries": 0},
        {"id": "b", "tries": 1},
    ]


def test_next_backlog_gives_up_after_max_tries():
    out = ex._next_backlog(
        ["x"],
        done={"x"},
        gate_skipped=set(),
        retry=["x"],
        tries={"x": ex.MAX_API_TRIES - 1},
    )
    assert out == []


def test_next_backlog_is_bounded(monkeypatch):
    monkeypatch.setattr(ex, "MAX_BACKLOG", 3)
    out = ex._next_backlog(list("abcde"), set(), set(), [], {})
    assert [o["id"] for o in out] == ["c", "d", "e"]


# ── DB: two passes through the orchestrator ─────────────────────────────────


@pytest.mark.db
def test_units_past_the_cap_are_extracted_next_run(conn, project_factory, monkeypatch):
    from cognify import gate
    from cognify.orchestrator import run_pass

    p = project_factory("extract_backlog")
    units = [str(uuid.uuid4()) for _ in range(3)]
    with conn.cursor() as cur:
        for u in units:
            cur.execute(
                "INSERT INTO devbrain.memory (project_id, kind, content, provenance_id) "
                "VALUES (%s, 'session_breadcrumb', %s, %s)",
                (p["id"], "x" * 500, u),
            )
    conn.commit()

    calls: list[str] = []

    def fake_extract(conn_, session_id, project_id, *, max_llm_calls=20, **kw):
        calls.append(session_id)
        return ex.ExtractResult(session_id=session_id, llm_calls=1)

    monkeypatch.setattr(ex, "extract_from_session", fake_extract)
    gate.reset_config(gate.GateConfig(mode="off"))
    try:
        first = run_pass(conn, "extract", p["id"], max_llm_calls=1)
        assert len(calls) == 1
        assert len(first.metadata["backlog"]) == 2
        # No new source rows arrive; the two leftovers still get extracted.
        second = run_pass(conn, "extract", p["id"], max_llm_calls=5)
    finally:
        gate.reset_config(None)
    assert sorted(calls) == sorted(units)
    assert second.metadata["backlog"] == []


@pytest.mark.db
def test_dry_run_does_not_move_the_watermark(conn, project_factory, monkeypatch):
    from cognify import gate
    from cognify.orchestrator import run_pass

    p = project_factory("extract_dry_wm")
    unit = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory (project_id, kind, content, provenance_id) "
            "VALUES (%s, 'session_breadcrumb', %s, %s)",
            (p["id"], "x" * 500, unit),
        )
    conn.commit()
    calls: list[str] = []
    monkeypatch.setattr(
        ex,
        "extract_from_session",
        lambda c, s, pid, **kw: (
            calls.append(s) or ex.ExtractResult(session_id=s, llm_calls=1)
        ),
    )
    gate.reset_config(gate.GateConfig(mode="off"))
    try:
        run_pass(conn, "extract", p["id"], dry_run=True)
        assert ex._last_successful_run(conn, "extract", p["id"]) is None
        run_pass(conn, "extract", p["id"])
    finally:
        gate.reset_config(None)
    assert calls == [unit]
