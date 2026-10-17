"""Which hosted LLM backends cognify may send memory/transcript text to.

Cognify passes (extract, fanout, edges, resummarize) can call two hosted
backends:

  codex      — the local `codex exec` CLI on a ChatGPT login
  anthropic  — the Anthropic API (API key or OAuth token)

Whether a backend may receive a deployment's data is a compliance decision
(e.g. a ChatGPT plan is not covered by a BAA, so a deployment holding PHI
must not use codex). ``cognify.allowed_backends`` records that decision per
deployment:

  cognify:
    allowed_backends: []                  # nothing hosted
    allowed_backends: [anthropic]         # Anthropic API only
    allowed_backends: [codex, anthropic]  # the default when unset

Env ``DEVBRAIN_COGNIFY_ALLOWED_BACKENDS`` (comma separated, "" = none)
overrides the yaml. The check is fail-closed: a value that is not a list
allows nothing, unknown names are ignored (so a typo blocks that backend),
and every refusal is logged. Callers treat a refusal as "not run": the unit
stays eligible for a later run instead of being marked done.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

BACKENDS = ("codex", "anthropic")

_CACHED: frozenset[str] | None = None
_REFUSED_LOGGED: set[tuple[str, str]] = set()


def parse_allowed(value: Any) -> frozenset[str]:
    """Normalize a yaml/env value. Fail-closed on anything unexpected."""
    if value is None:
        return frozenset(BACKENDS)
    if isinstance(value, str):
        items = [v.strip() for v in value.split(",") if v.strip()]
    elif isinstance(value, (list, tuple)):
        items = [str(v).strip() for v in value]
    else:
        logger.error(
            "cognify.allowed_backends must be a list; got %s — allowing no backend",
            type(value).__name__,
        )
        return frozenset()
    out = set()
    for item in items:
        name = item.lower()
        if name in BACKENDS:
            out.add(name)
        else:
            logger.error("cognify.allowed_backends: unknown backend %r ignored", item)
    return frozenset(out)


def load_allowed(cfg: dict | None = None) -> frozenset[str]:
    env = os.environ.get("DEVBRAIN_COGNIFY_ALLOWED_BACKENDS")
    if env is not None:
        return parse_allowed(env)
    if cfg is None:
        try:
            from config import load_config as _load  # factory/config.py

            cfg = _load()
        except Exception:  # noqa: BLE001
            logger.error("cognify: config unreadable — allowing no LLM backend")
            return frozenset()
    cognify = cfg.get("cognify") or {}
    if "allowed_backends" not in cognify:
        return frozenset(BACKENDS)
    return parse_allowed(cognify.get("allowed_backends"))


def allowed() -> frozenset[str]:
    global _CACHED
    if _CACHED is None:
        _CACHED = load_allowed()
    return _CACHED


def reset(value: frozenset[str] | None = None) -> None:
    """Tests: set (or clear) the cached allow-list."""
    global _CACHED
    _CACHED = value
    _REFUSED_LOGGED.clear()


def is_allowed(backend: str) -> bool:
    return backend in allowed()


def check(backend: str, pass_name: str) -> bool:
    """True when ``backend`` may be called. Logs each refused
    (backend, pass) pair once per process."""
    if is_allowed(backend):
        return True
    key = (backend, pass_name)
    if key not in _REFUSED_LOGGED:
        _REFUSED_LOGGED.add(key)
        logger.warning(
            "cognify %s: backend %r refused by cognify.allowed_backends=%s",
            pass_name,
            backend,
            sorted(allowed()),
        )
    return False


def backend_for_model(model: str | None) -> str:
    """The backend a model id routes to (mirrors extract._routes_to_codex)."""
    from cognify.extract import _routes_to_codex

    return "codex" if _routes_to_codex(model) else "anthropic"
