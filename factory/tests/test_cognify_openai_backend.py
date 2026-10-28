"""cognify backend=openai: routing, allow-list, no fallback, key, HTTP (no DB,
no network). The PHI-critical property under test: with backend=openai no
cognify text ever reaches codex or Anthropic."""

from __future__ import annotations

import io
import json
import logging
import urllib.error

import cognify.extract as ex
import pytest
from cognify import backends, edges, fanout, openai_api, resummarize, setup_launchd

FAKE_KEY = "sk-proj-FAKEFAKEFAKE1234"


# ── fakes ───────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _completion(content, *, prompt=1200, completion=300, cached=200, refusal=None):
    return {
        "choices": [{
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": content, "refusal": refusal},
        }],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "prompt_tokens_details": {"cached_tokens": cached},
        },
    }


def _http_error(code, body=b"{}", headers=None):
    return urllib.error.HTTPError(
        openai_api.API_URL, code, "err", headers or {}, io.BytesIO(body)
    )


class FakeHTTP:
    """Replays a script of responses (dict → 200 JSON, Exception → raised)."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(
            {"body": json.loads(req.data), "headers": dict(req.header_items()),
             "timeout": timeout, "url": req.full_url}
        )
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return _Resp(json.dumps(item).encode())


@pytest.fixture
def sleeps(monkeypatch):
    seen = []
    monkeypatch.setattr(openai_api, "_sleep", lambda s: seen.append(s))
    return seen


@pytest.fixture
def lht(monkeypatch, sleeps):
    """The Lighthouse recipe: backend openai, only openai allowed, a key."""
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    backends.reset(frozenset({"openai"}), selected="openai")


@pytest.fixture
def no_other_backend(monkeypatch):
    """Fail the test if anything reaches codex or Anthropic."""
    def boom(*a, **k):
        raise AssertionError("text sent to a non-openai backend")

    monkeypatch.setattr(ex.subprocess, "run", boom)
    monkeypatch.setattr(ex, "_llm_extract", boom)
    monkeypatch.setattr(ex, "_codex_extract", boom)
    monkeypatch.setattr(fanout, "_codex_classify", boom)
    monkeypatch.setattr(fanout, "_anthropic_classify", boom)
    monkeypatch.setattr(resummarize, "_call_sonnet", boom)
    try:
        import anthropic

        monkeypatch.setattr(anthropic, "Anthropic", boom)
    except ImportError:  # pragma: no cover
        pass


def _http(monkeypatch, *script) -> FakeHTTP:
    fake = FakeHTTP(*script)
    monkeypatch.setattr(openai_api, "_urlopen", fake)
    return fake


# ── backends: selection + allow-list ────────────────────────────────────────


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None), ("", None), ("  ", None),
        ("openai", "openai"), ("OpenAI", "openai"), ("codex", "codex"),
        ("anthropic", "anthropic"), ("opnai", backends.INVALID),
    ],
)
def test_parse_selected(value, expected):
    assert backends.parse_selected(value) == expected


def test_load_selected_env_overrides_yaml(monkeypatch):
    monkeypatch.delenv("DEVBRAIN_COGNIFY_BACKEND", raising=False)
    assert backends.load_selected({}) is None
    assert backends.load_selected({"cognify": {"backend": "openai"}}) == "openai"
    monkeypatch.setenv("DEVBRAIN_COGNIFY_BACKEND", "anthropic")
    assert backends.load_selected({"cognify": {"backend": "openai"}}) == "anthropic"


def test_openai_is_opt_in_only(monkeypatch):
    monkeypatch.delenv("DEVBRAIN_COGNIFY_ALLOWED_BACKENDS", raising=False)
    assert "openai" not in backends.load_allowed({})
    assert backends.load_allowed({"cognify": {"allowed_backends": ["openai"]}}) == {"openai"}
    assert backends.parse_allowed("openai,anthropic") == {"openai", "anthropic"}


def test_default_routing_unchanged():
    backends.reset(None, selected=None)
    assert backends.route("codex") == "codex"
    assert backends.route("gpt-5.6-luna") == "codex"
    assert backends.route("claude-sonnet-5") == "anthropic"
    assert backends.fallback_enabled()
    assert ex._backend_chain() == ["codex", "anthropic"]


def test_selection_routes_everything_and_disables_fallback():
    backends.reset(frozenset({"openai"}), selected="openai")
    for model in ("codex", "gpt-5.6-luna", "o3", "claude-sonnet-5"):
        assert backends.route(model) == "openai"
    assert not backends.fallback_enabled()
    assert not ex._fallback_configured()
    assert ex._backend_chain() == ["openai"]


# ── openai_api: model + key ─────────────────────────────────────────────────


def test_model_resolution_ignores_codex_model(monkeypatch):
    monkeypatch.setenv("DEVBRAIN_CODEX_MODEL", "gpt-codex-should-not-be-used")
    monkeypatch.setattr(openai_api, "_cognify_yaml", dict)
    assert openai_api.model_for("codex") == "gpt-5.6-luna"
    assert openai_api.model_for("claude-sonnet-5") == "gpt-5.6-luna"
    assert openai_api.model_for("gpt-5.6-terra") == "gpt-5.6-terra"
    monkeypatch.setattr(openai_api, "_cognify_yaml", lambda: {"openai_model": "gpt-yaml"})
    assert openai_api.model_for("codex") == "gpt-yaml"
    monkeypatch.setenv("DEVBRAIN_OPENAI_MODEL", "gpt-env")
    assert openai_api.model_for(None) == "gpt-env"


def test_key_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    assert openai_api.api_key() == FAKE_KEY


def test_key_missing_without_gcp(monkeypatch):
    calls = []
    monkeypatch.setattr(openai_api, "_run", lambda *a, **k: calls.append(a))
    assert openai_api.api_key() is None
    assert calls == []  # no gcloud without GOOGLE_APPLICATION_CREDENTIALS


class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_key_from_secret_manager_once(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/sa.json")
    monkeypatch.delenv("DEVBRAIN_OPENAI_SECRET_PROJECT", raising=False)
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return _Proc(out=FAKE_KEY + "\n")

    monkeypatch.setattr(openai_api, "_run", run)
    assert openai_api.api_key() == FAKE_KEY
    assert openai_api.api_key() == FAKE_KEY
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[1:5] == ["secrets", "versions", "access", "latest"]
    assert "--secret=openai-api-key" in cmd and "--project=brightbot-478117" in cmd


def test_secret_manager_retries_as_service_account(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/sa.json")
    monkeypatch.delenv("CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", raising=False)
    envs = []

    def run(cmd, env=None, **kw):
        envs.append(env)
        return _Proc(rc=1, err="no login") if env is None else _Proc(out=FAKE_KEY)

    monkeypatch.setattr(openai_api, "_run", run)
    assert openai_api.api_key() == FAKE_KEY
    assert envs[0] is None
    assert envs[1]["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"] == "/tmp/sa.json"


def test_secret_manager_failure_never_logs_key(monkeypatch, caplog):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/sa.json")
    monkeypatch.setenv("DEVBRAIN_OPENAI_SECRET_PROJECT", "proj-x")
    monkeypatch.setattr(
        openai_api, "_run",
        lambda cmd, **kw: _Proc(rc=1, err=f"denied for {FAKE_KEY}"),
    )
    with caplog.at_level(logging.DEBUG):
        assert openai_api.api_key() is None
    assert FAKE_KEY not in caplog.text and "proj-x" in caplog.text


# ── openai_api.complete: HTTP behaviour ─────────────────────────────────────


_SCHEMA = {"type": "object", "additionalProperties": False,
           "required": ["a"], "properties": {"a": {"type": "string"}}}


def test_complete_success_request_and_usage(monkeypatch, lht):
    fake = _http(monkeypatch, _completion('{"a": "x"}'))
    res = openai_api.complete("sys", "user", pass_name="t", json_schema=_SCHEMA)
    assert res.failure is None and res.parsed == {"a": "x"}
    assert res.usage == {"input_tokens": 1000, "output_tokens": 300,
                         "cache_read_tokens": 200, "cache_write_tokens": 0}
    req = fake.requests[0]
    assert req["url"] == "https://api.openai.com/v1/chat/completions"
    assert req["timeout"] == openai_api.TIMEOUT_S
    assert req["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
    body = req["body"]
    assert body["model"] == "gpt-5.6-luna" and body["store"] is False
    assert body["response_format"]["json_schema"]["strict"] is True
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert "reasoning_effort" not in body


def test_complete_refused_makes_no_call(monkeypatch, sleeps):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    backends.reset(frozenset({"codex", "anthropic"}), selected="openai")
    fake = _http(monkeypatch, _completion("{}"))
    res = openai_api.complete("s", "u", pass_name="t")
    assert res.failure == "refused" and fake.requests == []


def test_complete_no_key(monkeypatch, sleeps):
    backends.reset(frozenset({"openai"}), selected="openai")
    fake = _http(monkeypatch, _completion("{}"))
    res = openai_api.complete("s", "u", pass_name="t")
    assert res.failure == "no_auth" and fake.requests == []


def test_429_retried_with_retry_after(monkeypatch, lht, sleeps):
    fake = _http(
        monkeypatch,
        _http_error(429, headers={"Retry-After": "7"}),
        _http_error(503),
        _completion('{"a": "ok"}'),
    )
    res = openai_api.complete("s", "u", pass_name="t", json_schema=_SCHEMA)
    assert res.failure is None and res.parsed == {"a": "ok"}
    assert len(fake.requests) == 3
    assert sleeps == [7.0, 5]


def test_5xx_exhausts_retries_as_api(monkeypatch, lht, sleeps):
    fake = _http(monkeypatch, _http_error(500))
    res = openai_api.complete("s", "u", pass_name="t")
    assert res.failure == "api" and "HTTP 500" in res.detail
    assert len(fake.requests) == openai_api.MAX_ATTEMPTS
    assert len(sleeps) == openai_api.MAX_ATTEMPTS - 1


def test_network_error_retried(monkeypatch, lht, sleeps):
    fake = _http(monkeypatch, urllib.error.URLError("reset"), TimeoutError("slow"),
                 _completion("summary text"))
    res = openai_api.complete("s", "u", pass_name="t")
    assert res.failure is None and res.text == "summary text"
    assert len(fake.requests) == 3


def test_4xx_not_retried_and_key_masked(monkeypatch, lht, sleeps, caplog):
    body = json.dumps({"error": {"message": f"Incorrect API key provided: {FAKE_KEY}"}})
    fake = _http(monkeypatch, _http_error(401, body.encode()))
    with caplog.at_level(logging.DEBUG):
        res = openai_api.complete("s", "u", pass_name="t")
    assert res.failure == "api" and "HTTP 401" in res.detail
    assert len(fake.requests) == 1 and sleeps == []
    assert FAKE_KEY not in res.detail and FAKE_KEY not in caplog.text


@pytest.mark.parametrize(
    "response,failure",
    [
        (_completion("not json at all"), "json_parse"),
        (_completion(None, refusal="I can't help"), "json_parse"),
        (_completion(""), "empty"),
    ],
)
def test_output_failures(monkeypatch, lht, response, failure):
    _http(monkeypatch, response)
    res = openai_api.complete("s", "u", pass_name="t", json_schema=_SCHEMA)
    assert res.failure == failure
    assert res.usage["output_tokens"] == 300  # the call happened: keep spend


def test_reasoning_effort_sent_when_configured(monkeypatch, lht):
    monkeypatch.setenv("DEVBRAIN_OPENAI_REASONING_EFFORT", "low")
    fake = _http(monkeypatch, _completion("x"))
    openai_api.complete("s", "u", pass_name="t")
    assert fake.requests[0]["body"]["reasoning_effort"] == "low"


# ── extract ─────────────────────────────────────────────────────────────────


_BODY = "We chose the platform API. Retries back off on 503."


def _session(monkeypatch):
    monkeypatch.setattr(
        ex, "_load_session_chunks", lambda c, s, p: [{"title": "t", "content": _BODY}]
    )
    written = []
    monkeypatch.setattr(
        ex, "_upsert_memory",
        lambda conn, **kw: (written.append(kw) or f"m{len(written)}", True),
    )
    spend = []
    monkeypatch.setattr(ex, "record_spend", lambda conn, **kw: spend.append(kw))
    return written, spend


_EXTRACTED = json.dumps({
    "lessons": [{"title": "L1", "content": "lesson body",
                 "quote": "Retries back off on 503."}],
    "decisions": [{"title": "D1", "content": "decision body",
                   "quote": "We chose the platform API."}],
})


def test_extract_parses_canned_response_and_records_spend(
    monkeypatch, lht, no_other_backend
):
    monkeypatch.setenv("DEVBRAIN_CODEX_MODEL", "gpt-codex-should-not-be-used")
    written, spend = _session(monkeypatch)
    fake = _http(monkeypatch, _completion(_EXTRACTED))
    res = ex.extract_from_session(object(), "s1", "p1", model="codex")
    assert res.failure is None
    assert (res.lessons_created, res.decisions_created, res.llm_calls) == (1, 1, 1)
    assert [w["kind"] for w in written] == ["lesson", "decision"]
    body = fake.requests[0]["body"]
    assert body["model"] == "gpt-5.6-luna"
    assert body["response_format"]["json_schema"]["schema"] == ex._CODEX_OUTPUT_SCHEMA
    assert spend[0]["model"] == "gpt-5.6-luna" and spend[0]["input_tokens"] == 1000
    assert spend[0]["output_tokens"] == 300 and spend[0]["cache_read_tokens"] == 200


def test_gpt_model_id_not_routed_to_codex(monkeypatch, lht, no_other_backend):
    _session(monkeypatch)
    fake = _http(monkeypatch, _completion(_EXTRACTED))
    res = ex.extract_from_session(object(), "s1", "p1", model="gpt-5.6-terra")
    assert res.failure is None
    assert fake.requests[0]["body"]["model"] == "gpt-5.6-terra"


def test_openai_failure_never_falls_back(monkeypatch, sleeps, no_other_backend):
    """Even with anthropic + codex allowed and a fallback model configured."""
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    backends.reset(frozenset({"openai", "anthropic", "codex"}), selected="openai")
    monkeypatch.setattr(ex, "_FALLBACK_MODEL", "claude-sonnet-5")
    _session(monkeypatch)
    _http(monkeypatch, _http_error(503))
    res = ex.extract_from_session(object(), "s1", "p1", model="codex")
    assert res.failure == "api" and res.lessons_created == 0


def test_openai_not_allowed_is_refused_without_fallback(monkeypatch, no_other_backend):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    backends.reset(frozenset({"anthropic", "codex"}), selected="openai")
    _session(monkeypatch)
    fake = _http(monkeypatch, _completion(_EXTRACTED))
    res = ex.extract_from_session(object(), "s1", "p1", model="codex")
    assert res.failure == "refused" and res.llm_calls == 0
    assert fake.requests == []


def test_missing_key_keeps_session_eligible(monkeypatch, no_other_backend):
    backends.reset(frozenset({"openai"}), selected="openai")
    _session(monkeypatch)
    res = ex.extract_from_session(object(), "s1", "p1")
    assert res.failure == "refused" and res.llm_calls == 0


class _NoSQL:
    def cursor(self):
        raise AssertionError("a refused pass must not query or extract")


def test_pass_refused_up_front_when_openai_not_allowed(monkeypatch, caplog):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    backends.reset(frozenset({"anthropic", "codex"}), selected="openai")
    with caplog.at_level("WARNING"):
        res = ex.ExtractPass().run(_NoSQL(), "p1")
    assert res.metadata["refused"] is True and res.llm_calls == 0
    assert "'openai' refused" in caplog.text


def test_pass_refused_up_front_without_key():
    backends.reset(frozenset({"openai"}), selected="openai")
    res = ex.ExtractPass().run(_NoSQL(), "p1")
    assert res.metadata["refused"] is True


def test_invalid_backend_refuses_everything(monkeypatch, no_other_backend):
    backends.reset(frozenset({"openai", "anthropic", "codex"}), selected=backends.INVALID)
    _session(monkeypatch)
    res = ex.extract_from_session(object(), "s1", "p1")
    assert res.failure == "refused"
    assert ex.ExtractPass().run(_NoSQL(), "p1").metadata["refused"] is True


def test_selected_codex_has_no_anthropic_fallback(monkeypatch):
    backends.reset(frozenset({"codex", "anthropic"}), selected="codex")
    _session(monkeypatch)
    monkeypatch.setattr(ex, "_FALLBACK_MODEL", "claude-sonnet-5")
    monkeypatch.setattr(ex, "_codex_extract", lambda c, model=None: {
        "lessons": [], "decisions": [], "_usage": {}, "_failure": "api"})

    def no_anthropic(*a, **k):
        raise AssertionError("fallback ran under an explicit backend")

    monkeypatch.setattr(ex, "_llm_extract", no_anthropic)
    assert ex.extract_from_session(object(), "s1", "p1").failure == "api"


# ── fanout ──────────────────────────────────────────────────────────────────


def _fanout_inputs(monkeypatch):
    monkeypatch.setattr(fanout, "_load_active_taxonomy", lambda conn: [
        {"slug": "brightbot", "name": "BrightBot", "description": "d"},
        {"slug": "devbrain", "name": "DevBrain", "description": "d"},
    ])
    monkeypatch.setattr(fanout, "_load_session_content", lambda conn, sid: "session text")


def test_fanout_parses_canned_response(monkeypatch, lht, no_other_backend):
    _fanout_inputs(monkeypatch)
    payload = {"per_project": [
        {"project_slug": "brightbot", "session_relevance": 0.9,
         "section_count": 2, "focused_summary": "x" * 250},
        {"project_slug": "devbrain", "session_relevance": 0.1,
         "section_count": 0, "focused_summary": "below threshold"},
    ]}
    fake = _http(monkeypatch, _completion(json.dumps(payload)))
    res = fanout.classify_session("s1", object(), model="codex")
    assert res.failure is None
    assert [p.project_slug for p in res.per_project] == ["brightbot"]
    assert res.model == "gpt-5.6-luna" and res.usage["input_tokens"] == 1000
    body = fake.requests[0]["body"]
    assert body["response_format"]["json_schema"]["schema"] == fanout._FANOUT_OUTPUT_SCHEMA


def test_fanout_failure_never_falls_back(monkeypatch, sleeps, no_other_backend):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    backends.reset(frozenset({"openai", "anthropic", "codex"}), selected="openai")
    _fanout_inputs(monkeypatch)
    _http(monkeypatch, _http_error(500))
    res = fanout.classify_session("s1", object(), model="codex")
    assert res.failure == "api" and res.per_project == []


def test_fanout_refused_when_openai_not_allowed(monkeypatch, no_other_backend):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    backends.reset(frozenset({"anthropic", "codex"}), selected="openai")
    _fanout_inputs(monkeypatch)
    fake = _http(monkeypatch, _completion("{}"))
    assert fanout.classify_session("s1", object()).failure == "refused"
    assert fake.requests == []


# ── edges + resummarize ─────────────────────────────────────────────────────


def test_edges_judge_on_openai(monkeypatch, lht, no_other_backend):
    fake = _http(monkeypatch, _completion('{"contradicts": true}'))
    flag, usage, failure = edges._llm_judge_contradiction("always X", "never X")
    assert failure is None
    assert flag is True and usage["output_tokens"] == 300
    assert fake.requests[0]["body"]["model"] == "gpt-5.6-luna"
    _http(monkeypatch, _completion('{"contradicts": false}'))
    assert edges._llm_judge_contradiction("a", "b")[0] is False


def test_edges_refused_and_codex_selected(monkeypatch, tmp_path, no_other_backend):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    monkeypatch.setenv("DEVBRAIN_CODEX_HOME", str(tmp_path / "no-codex-home"))
    fake = _http(monkeypatch, _completion('{"contradicts": true}'))
    backends.reset(frozenset({"anthropic"}), selected="openai")
    assert edges._llm_judge_contradiction("a", "b")[::2] == (False, "refused")
    # backend=codex: codex only; without its isolated home nothing is sent.
    backends.reset(frozenset({"codex", "anthropic"}), selected="codex")
    assert edges._llm_judge_contradiction("a", "b")[::2] == (False, "no_auth")
    assert fake.requests == []


def test_resummarize_on_openai(monkeypatch, lht, no_other_backend):
    fake = _http(monkeypatch, _completion("The agent fixed the router."))
    text, _usage, failure = resummarize._summarize("raw transcript", "gpt-5.6-luna")
    assert failure is None and text == "The agent fixed the router."
    assert "response_format" not in fake.requests[0]["body"]


def test_resummarize_refused_and_codex_selected(monkeypatch, tmp_path, no_other_backend):
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    monkeypatch.setenv("DEVBRAIN_CODEX_HOME", str(tmp_path / "no-codex-home"))
    fake = _http(monkeypatch, _completion("x"))
    backends.reset(frozenset({"anthropic"}), selected="openai")
    assert resummarize._summarize("raw", "claude-sonnet-5")[2] == "refused"
    # backend=codex: codex only, no Anthropic fallback; the missing isolated
    # CODEX_HOME reads as no_auth and nothing is sent anywhere.
    backends.reset(frozenset({"codex", "anthropic"}), selected="codex")
    assert resummarize._summarize("raw", "claude-sonnet-5")[2] == "no_auth"
    assert fake.requests == []


def test_resummarize_api_failure_label(monkeypatch, lht):
    _http(monkeypatch, _http_error(500))
    assert resummarize._summarize("raw", "x")[2] == "api:openai"


# ── launchd installer ───────────────────────────────────────────────────────


def test_openai_env_only_passed_through_when_selected(monkeypatch):
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/Users/x/sa.json")
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    backends.reset(None, selected=None)
    assert setup_launchd.resolve_openai_env() == {}
    backends.reset(None, selected="openai")
    pinned = {
        "DEVBRAIN_COGNIFY_BACKEND": "openai",
        "DEVBRAIN_COGNIFY_ALLOWED_BACKENDS": "openai",
    }
    env = setup_launchd.resolve_openai_env()
    assert env == {"GOOGLE_APPLICATION_CREDENTIALS": "/Users/x/sa.json", **pinned}
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS")
    assert setup_launchd.resolve_openai_env() == {"OPENAI_API_KEY": FAKE_KEY, **pinned}


def test_render_with_openai_env_and_no_anthropic_credential():
    text = (setup_launchd._TEMPLATE_DIR / "com.devbrain.cognify-extract.plist").read_text()
    out = setup_launchd.render_plist(
        text, devbrain_home="/d", user="u", project_slug="brightbot",
        credential=None,
        extra_env={"GOOGLE_APPLICATION_CREDENTIALS": "/Users/u/sa & key.json"},
    )
    assert "<key>GOOGLE_APPLICATION_CREDENTIALS</key>" in out
    assert "<string>/Users/u/sa &amp; key.json</string>" in out
    assert "@CREDENTIAL_ENV_BLOCK@" not in out
    assert "ANTHROPIC_API_KEY" not in out


def test_install_pins_openai_and_drops_anthropic_credential(monkeypatch, tmp_path):
    """Selection from the installing shell must survive into the plists:
    the jobs run with openai pinned and no Anthropic credential, so a later
    yaml drift cannot route PHI to codex or Anthropic."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-appear")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/Users/x/sa.json")
    backends.reset(None, selected="openai")
    paths = setup_launchd.install_cognify_launchd(
        project_slug="brightbot", devbrain_home="/d", user="u",
        target_dir=tmp_path, reload=False,
    )
    cred_names = set(setup_launchd._CRED_PLISTS)
    seen = 0
    for p in paths:
        text = p.read_text()
        assert "sk-ant-should-not-appear" not in text
        if p.name in cred_names:
            seen += 1
            assert "<key>DEVBRAIN_COGNIFY_BACKEND</key>\n        <string>openai</string>" in text
            assert "<key>DEVBRAIN_COGNIFY_ALLOWED_BACKENDS</key>\n        <string>openai</string>" in text
    assert seen == len(cred_names)
