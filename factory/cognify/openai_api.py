"""OpenAI platform API client for cognify's ``openai`` backend.

Used by every cognify LLM pass (extract, fanout, edges, resummarize) when
``cognify.backend: openai`` — see cognify/backends.py. Plain HTTPS to the
Chat Completions endpoint (stdlib urllib, no SDK): it is the endpoint
ingest/close_orphan_sessions.py already runs against the same key and model,
and ``response_format: json_schema`` (strict) gives the same schema-
constrained JSON the codex path gets from ``--output-schema``, so extract and
fanout reuse their codex schemas unchanged.

Model: ``DEVBRAIN_OPENAI_MODEL`` > yaml ``cognify.openai_model`` >
gpt-5.6-luna. A caller-supplied OpenAI id (gpt-*/o3/o4, e.g. a CLI
``--model gpt-5.6-terra``) wins; the "codex" sentinel and claude-* ids map
to the configured model. ``DEVBRAIN_CODEX_MODEL`` is never read here.

Key: ``OPENAI_API_KEY``; when unset and ``GOOGLE_APPLICATION_CREDENTIALS``
is set, it is read once per process from GCP Secret Manager with
``gcloud secrets versions access latest --secret=openai-api-key
--project=$DEVBRAIN_OPENAI_SECRET_PROJECT`` (default brightbot-478117;
secret name env ``DEVBRAIN_OPENAI_SECRET``; gcloud binary env
``DEVBRAIN_GCLOUD_BIN``). gcloud uses its own login first and, if that
fails, the service-account file (CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE).
The fetch lives here rather
than in a wrapper script because cognify's launchd jobs (and manual
``devbrain cognify*`` runs) start python directly — so every entry point
gets the key the same way and the key is never written into a plist or
env file. The key is never logged; error bodies are masked.

Failure labels match the codex/anthropic paths: ``refused`` (openai not
in cognify.allowed_backends), ``no_auth`` (no key), ``api`` (HTTP/network
error after retries), ``json_parse`` (unusable output), ``empty``.
429/408/409/5xx and network errors are retried with backoff (Retry-After
honoured, capped); other 4xx are not.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_SECRET = "openai-api-key"
DEFAULT_SECRET_PROJECT = "brightbot-478117"

TIMEOUT_S = int(os.environ.get("DEVBRAIN_OPENAI_TIMEOUT_S", "120"))
MAX_ATTEMPTS = 4
BACKOFF_S = (2, 5, 15)
RETRY_AFTER_CAP_S = 60
RETRY_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})

# Injection points for tests (no network, no sleeping).
_urlopen = urllib.request.urlopen
_sleep = time.sleep
_run = subprocess.run

_KEY_CACHE: tuple[str | None] | None = None
_KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-*]{6,}")


def _empty_usage() -> dict:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


@dataclass
class Result:
    model: str
    text: str | None = None
    parsed: dict | None = None
    usage: dict = field(default_factory=_empty_usage)
    failure: str | None = None
    detail: str | None = None


def _cognify_yaml() -> dict:
    try:
        from config import load_config as _load  # noqa: PLC0415 — factory/config.py

        return _load().get("cognify") or {}
    except Exception:  # noqa: BLE001
        return {}


def configured_model() -> str:
    return (
        os.environ.get("DEVBRAIN_OPENAI_MODEL")
        or str(_cognify_yaml().get("openai_model") or "")
        or DEFAULT_MODEL
    )


def is_openai_model(model: str | None) -> bool:
    if not model:
        return False
    m = model.lower()
    return m.startswith(("gpt-", "gpt5", "o3", "o4"))


def model_for(requested: str | None) -> str:
    """The OpenAI model a call asking for ``requested`` uses."""
    return requested if is_openai_model(requested) else configured_model()


def _reasoning_effort() -> str:
    env = os.environ.get("DEVBRAIN_OPENAI_REASONING_EFFORT")
    if env is not None:
        return env.strip()
    return str(_cognify_yaml().get("openai_reasoning_effort") or "").strip()


def _mask(text: str) -> str:
    return _KEY_RE.sub("sk-***", text or "")


def _fetch_secret() -> str | None:
    gcloud = (
        os.environ.get("DEVBRAIN_GCLOUD_BIN")
        or shutil.which("gcloud")
        or "/opt/homebrew/bin/gcloud"
    )
    secret = os.environ.get("DEVBRAIN_OPENAI_SECRET") or DEFAULT_SECRET
    project = os.environ.get("DEVBRAIN_OPENAI_SECRET_PROJECT") or DEFAULT_SECRET_PROJECT
    cmd = [
        gcloud, "secrets", "versions", "access", "latest",
        f"--secret={secret}", f"--project={project}",
    ]
    # gcloud authenticates with its own login, not GOOGLE_APPLICATION_
    # CREDENTIALS. Try that login first (what ingest/launchd/run-closure-
    # scan.sh does); if it fails, retry once as the service-account file.
    attempts = [None]
    gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if gac and not os.environ.get("CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"):
        attempts.append({**os.environ, "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE": gac})
    proc = None
    for env in attempts:
        try:
            proc = _run(cmd, capture_output=True, text=True, timeout=60, env=env)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.error(
                "cognify openai: gcloud secret fetch failed: %s", type(exc).__name__
            )
            return None
        if proc.returncode == 0:
            break
    if proc is None or proc.returncode != 0:
        logger.error(
            "cognify openai: gcloud secret %s/%s fetch failed (rc=%s): %s",
            project, secret, proc.returncode, _mask((proc.stderr or "")[-300:]),
        )
        return None
    return (proc.stdout or "").strip() or None


def api_key() -> str | None:
    """OPENAI_API_KEY, else the Secret Manager secret (fetched once)."""
    global _KEY_CACHE
    env = os.environ.get("OPENAI_API_KEY")
    if env:
        return env.strip()
    if _KEY_CACHE is None:
        key = None
        if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            key = _fetch_secret()
        if key is None:
            logger.error(
                "cognify openai: no API key (set OPENAI_API_KEY, or "
                "GOOGLE_APPLICATION_CREDENTIALS for Secret Manager)"
            )
        _KEY_CACHE = (key,)
    return _KEY_CACHE[0]


def reset() -> None:
    """Tests: forget the cached key."""
    global _KEY_CACHE
    _KEY_CACHE = None


def _usage(data: dict) -> dict:
    u = data.get("usage") or {}
    prompt = int(u.get("prompt_tokens") or 0)
    cached = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    return {
        # Same convention as the Anthropic paths: input_tokens excludes
        # cache reads (OpenAI's prompt_tokens includes them).
        "input_tokens": max(prompt - cached, 0),
        "output_tokens": int(u.get("completion_tokens") or 0),
        "cache_read_tokens": cached,
        "cache_write_tokens": 0,
    }


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", "replace")
        msg = (json.loads(body).get("error") or {}).get("message") or body
    except Exception:  # noqa: BLE001
        msg = ""
    return _mask(f"HTTP {exc.code}: {msg}"[:400])


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    try:
        val = float((exc.headers or {}).get("Retry-After"))
    except (TypeError, ValueError):
        return None
    return max(0.0, min(val, RETRY_AFTER_CAP_S))


def _parse_json(text: str) -> dict | None:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        from cognify.extract import _parse_json_with_fallbacks  # noqa: PLC0415

        obj = _parse_json_with_fallbacks(text)
    return obj if isinstance(obj, dict) else None


def complete(
    system: str,
    user: str,
    *,
    pass_name: str,
    model: str | None = None,
    json_schema: dict | None = None,
    schema_name: str = "result",
    max_output_tokens: int = 16_000,
) -> Result:
    """One Chat Completions call. Never raises; failures are labelled."""
    from cognify import backends  # noqa: PLC0415

    use_model = model_for(model)
    res = Result(model=use_model)
    if not backends.check("openai", pass_name):
        res.failure, res.detail = "refused", "openai not in cognify.allowed_backends"
        return res
    key = api_key()
    if not key:
        res.failure, res.detail = "no_auth", "no OpenAI API key"
        return res

    body: dict[str, Any] = {
        "model": use_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_completion_tokens": max_output_tokens,
        "store": False,
    }
    effort = _reasoning_effort()
    if effort and effort.lower() != "inherit":
        body["reasoning_effort"] = effort
    if json_schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": json_schema},
        }
    payload = json.dumps(body).encode()

    data: dict | None = None
    detail = None
    for attempt in range(MAX_ATTEMPTS):
        req = urllib.request.Request(
            API_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
        )
        wait: float | None = None
        try:
            with _urlopen(req, timeout=TIMEOUT_S) as resp:
                raw = resp.read()
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                detail = "non-JSON API response body"
            break
        except urllib.error.HTTPError as exc:
            detail = _error_detail(exc)
            if exc.code not in RETRY_STATUS:
                break
            wait = _retry_after(exc)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            detail = _mask(f"{type(exc).__name__}: {exc}"[:300])
        if attempt + 1 < MAX_ATTEMPTS:
            if wait is None:
                wait = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
            logger.warning(
                "cognify %s(openai): %s (attempt %d/%d); retrying in %.0fs",
                pass_name, detail, attempt + 1, MAX_ATTEMPTS, wait,
            )
            _sleep(wait)

    if data is None:
        logger.warning("cognify %s(openai): -m %s failed: %s", pass_name, use_model, detail)
        res.failure, res.detail = "api", detail or "OpenAI API call failed"
        return res

    res.usage = _usage(data)
    choice = (data.get("choices") or [{}])[0] or {}
    message = choice.get("message") or {}
    if message.get("refusal"):
        res.failure = "json_parse" if json_schema is not None else "empty"
        res.detail = "model refusal"
        return res
    text = (message.get("content") or "").strip()
    res.text = text
    if not text:
        res.failure = "empty"
        res.detail = f"empty output (finish_reason={choice.get('finish_reason')})"
        return res
    if json_schema is not None:
        res.parsed = _parse_json(text)
        if res.parsed is None:
            res.failure = "json_parse"
            res.detail = (
                f"unparseable output (finish_reason={choice.get('finish_reason')})"
            )
    return res
