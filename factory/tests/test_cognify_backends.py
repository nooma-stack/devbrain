"""cognify.allowed_backends + the isolated cognify CODEX_HOME (no DB)."""

from __future__ import annotations

import json
from pathlib import Path

import cognify.extract as ex
import pytest
from cognify import backends, codex_home

# ── allowed_backends ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, {"codex", "anthropic"}),
        ([], set()),
        (["anthropic"], {"anthropic"}),
        (["Codex", "anthropic"], {"codex", "anthropic"}),
        (["anthropc"], set()),  # typo blocks, never widens
        ("codex,anthropic", {"codex", "anthropic"}),
        ("", set()),
        ({"codex": True}, set()),  # not a list: fail-closed
        (7, set()),
    ],
)
def test_parse_allowed(value, expected):
    assert backends.parse_allowed(value) == expected


def test_load_allowed_yaml_and_env(monkeypatch):
    monkeypatch.delenv("DEVBRAIN_COGNIFY_ALLOWED_BACKENDS", raising=False)
    assert backends.load_allowed({}) == {"codex", "anthropic"}
    assert backends.load_allowed({"cognify": {"allowed_backends": []}}) == set()
    assert backends.load_allowed({"cognify": {"allowed_backends": None}}) == {
        "codex",
        "anthropic",
    }
    monkeypatch.setenv("DEVBRAIN_COGNIFY_ALLOWED_BACKENDS", "anthropic")
    assert backends.load_allowed({"cognify": {"allowed_backends": []}}) == {"anthropic"}


def test_refusal_is_logged_once(caplog):
    backends.reset(frozenset())
    with caplog.at_level("WARNING"):
        assert not backends.check("codex", "extract")
        assert not backends.check("codex", "extract")
    assert sum("refused" in r.message for r in caplog.records) == 1


# ── codex home ──────────────────────────────────────────────────────────────


def _home(tmp_path: Path, *, auth=True, extra="", name="h") -> Path:
    home = tmp_path / name
    codex_home.init(home)
    if auth:
        (home / "auth.json").write_text("{}")
    if extra:
        with open(home / "config.toml", "a") as fh:
            fh.write(extra)
    return home


def test_template_passes_check(tmp_path):
    ok, reason = codex_home.check(_home(tmp_path))
    assert ok, reason
    assert oct((tmp_path / "h").stat().st_mode & 0o777) == "0o700"


def test_missing_dir_auth_or_config(tmp_path):
    assert not codex_home.check(tmp_path / "nope")[0]
    assert "auth.json" in codex_home.check(_home(tmp_path, auth=False, name="a"))[1]
    home = _home(tmp_path, name="b")
    (home / "config.toml").unlink()
    assert "config.toml" in codex_home.check(home)[1]


@pytest.mark.parametrize(
    "extra,needle",
    [
        ('\n[mcp_servers.brain]\ncommand = "x"\n', "mcp_servers"),
        ('\n[plugins."slack@x"]\nenabled = true\n', "plugins"),
        ('notify = ["x"]\n', "notify"),
    ],
)
def test_tools_in_config_are_refused(tmp_path, extra, needle):
    home = tmp_path / "h"
    codex_home.init(home)
    (home / "auth.json").write_text("{}")
    cfg = (home / "config.toml").read_text()
    # Top-level keys must come before [features].
    head, tail = cfg.split("[features]", 1)
    (home / "config.toml").write_text(
        (extra + "\n" + head + "[features]" + tail)
        if not extra.startswith("\n")
        else (head + "[features]" + tail + extra)
    )
    ok, reason = codex_home.check(home)
    assert not ok and needle in reason


def test_feature_switched_back_on_is_refused(tmp_path):
    home = _home(tmp_path)
    cfg = (home / "config.toml").read_text().replace("apps = false", "apps = true")
    (home / "config.toml").write_text(cfg)
    ok, reason = codex_home.check(home)
    assert not ok and "apps" in reason


def test_interactive_home_is_refused(tmp_path, monkeypatch):
    fake_home = tmp_path / "user"
    monkeypatch.setenv("HOME", str(fake_home))
    interactive = fake_home / ".codex"
    codex_home.init(interactive)
    (interactive / "auth.json").write_text("{}")
    ok, reason = codex_home.check(interactive)
    assert not ok and "interactive" in reason


def test_init_does_not_overwrite(tmp_path):
    home = _home(tmp_path)
    with pytest.raises(FileExistsError):
        codex_home.init(home)
    codex_home.init(home, force=True)


def test_env_for_points_codex_at_the_home(tmp_path):
    assert codex_home.env_for(tmp_path)["CODEX_HOME"] == str(tmp_path)


# ── extract wiring ──────────────────────────────────────────────────────────


class _Proc:
    returncode, stdout, stderr = 0, "", ""


def _fake_codex(monkeypatch, tmp_path):
    fake_bin = tmp_path / "codex"
    fake_bin.write_text("#!/bin/sh\n")
    monkeypatch.setattr(ex, "_CODEX_BIN", str(fake_bin))
    seen = {}

    def run(cmd, **kw):
        seen["env"] = kw.get("env")
        out = cmd[cmd.index("-o") + 1]
        Path(out).write_text(
            json.dumps({"lessons": [{"title": "t", "content": "c"}], "decisions": []})
        )
        return _Proc()

    monkeypatch.setattr(ex.subprocess, "run", run)
    return seen


def test_codex_runs_with_the_isolated_home(monkeypatch, tmp_path):
    seen = _fake_codex(monkeypatch, tmp_path)
    res = ex._codex_extract("text")
    assert res.get("_failure") is None
    assert seen["env"]["CODEX_HOME"] == str(codex_home.resolve_home())


def test_codex_refused_without_safe_home(monkeypatch, tmp_path):
    seen = _fake_codex(monkeypatch, tmp_path)
    monkeypatch.setenv("DEVBRAIN_CODEX_HOME", str(tmp_path / "missing"))
    res = ex._codex_extract("text")
    assert res["_failure"] == "refused" and "isolation" in res["_failure_detail"]
    assert seen == {}  # codex never ran


def test_codex_refused_by_allow_list(monkeypatch, tmp_path):
    seen = _fake_codex(monkeypatch, tmp_path)
    backends.reset(frozenset({"anthropic"}))
    assert ex._codex_extract("text")["_failure"] == "refused"
    assert seen == {}


def test_anthropic_refused_by_allow_list():
    backends.reset(frozenset({"codex"}))
    assert ex._llm_extract("text", model="claude-x")["_failure"] == "refused"


def _session(monkeypatch):
    monkeypatch.setattr(
        ex, "_load_session_chunks", lambda c, s, p: [{"title": "t", "content": "body"}]
    )


def test_codex_refused_goes_to_allowed_fallback(monkeypatch):
    _session(monkeypatch)
    backends.reset(frozenset({"anthropic"}))
    monkeypatch.setattr(ex, "_FALLBACK_MODEL", "claude-test")
    called = {}

    def fake_llm(content, *, max_llm_calls=1, model=None):
        called["model"] = model
        return {"lessons": [], "decisions": [], "_usage": {}}

    monkeypatch.setattr(ex, "_llm_extract", fake_llm)
    res = ex.extract_from_session(object(), "s1", "p1", model="codex")
    assert called["model"] == "claude-test" and res.failure is None


def test_nothing_allowed_is_refused_without_a_call(monkeypatch):
    _session(monkeypatch)
    backends.reset(frozenset())
    monkeypatch.setattr(ex, "_FALLBACK_MODEL", "claude-test")
    res = ex.extract_from_session(object(), "s1", "p1", model="codex")
    assert res.failure == "refused" and res.llm_calls == 0


def test_pass_refuses_up_front_when_nothing_is_usable(monkeypatch):
    backends.reset(frozenset())

    class _NoSQL:
        def cursor(self):
            raise AssertionError("a refused pass must not query or extract")

    res = ex.ExtractPass().run(_NoSQL(), "p1")
    assert res.metadata["refused"] is True and res.llm_calls == 0


# ── fanout / edges / resummarize guards ─────────────────────────────────────


def test_other_passes_honour_the_allow_list(monkeypatch, tmp_path):
    from cognify import edges, fanout, resummarize

    backends.reset(frozenset())
    assert fanout._codex_classify("s", "u")[1] == "refused"
    assert fanout._anthropic_classify("s", "u", model="claude-x")[1] == "refused"
    assert edges._llm_judge_contradiction("a", "b")[0] is False
    assert resummarize._call_sonnet("raw", "claude-x")[2] == "refused"
