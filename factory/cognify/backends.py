"""Which hosted LLM backends cognify may send memory/transcript text to.

Cognify passes (extract, fanout, edges, resummarize) can call three hosted
backends:

  codex      — the local `codex exec` CLI on a ChatGPT login
  anthropic  — the Anthropic API (API key or OAuth token)
  openai     — the OpenAI platform API (api.openai.com, API key)

Whether a backend may receive a deployment's data is a compliance decision
(e.g. a ChatGPT plan is not covered by a BAA, so a deployment holding PHI
must not use codex; an OpenAI *platform* org under a BAA may be). Two keys
record that decision per deployment:

  cognify:
    allowed_backends: []                  # nothing hosted
    allowed_backends: [anthropic]         # Anthropic API only
    allowed_backends: [openai]            # OpenAI platform API only
    allowed_backends: [codex, anthropic]  # the default when unset
    backend: openai                       # unset = route by model id

``allowed_backends`` — env ``DEVBRAIN_COGNIFY_ALLOWED_BACKENDS`` (comma
separated, "" = none) overrides the yaml. The check is fail-closed: a value
that is not a list allows nothing, unknown names are ignored (so a typo
blocks that backend), and every refusal is logged. Callers treat a refusal
as "not run": the unit stays eligible for a later run instead of being
marked done. ``openai`` is never allowed by default: it must be listed.

``backend`` — env ``DEVBRAIN_COGNIFY_BACKEND`` overrides the yaml. Unset
keeps the model-based routing (codex for "codex"/gpt-*/o3/o4 ids, Anthropic
otherwise, with extract/fanout/resummarize/edges falling back from codex to
Anthropic). Set, EVERY pass uses exactly that backend and nothing else: no
fallback to another backend. An unknown value selects nothing, so every
pass is refused. With ``backend: openai`` the model is
``cognify.openai_model`` (env ``DEVBRAIN_OPENAI_MODEL``, default
gpt-5.6-luna); ``DEVBRAIN_CODEX_MODEL`` / ``cognify.codex_model`` are not
used. See cognify/openai_api.py for the key.

Lighthouse (PHI, OpenAI BAA on the API platform only):

  cognify:
    backend: openai
    openai_model: gpt-5.6-luna
    allowed_backends: [openai]
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

BACKENDS = ("codex", "anthropic", "openai")

# Allowed when cognify.allowed_backends is unset. openai is opt-in only.
DEFAULT_ALLOWED = frozenset({"codex", "anthropic"})

# backend value that names no real backend: never allowed, so every
# check() on it refuses (fail-closed on a typo in cognify.backend).
INVALID = "<invalid>"

_CACHED: frozenset[str] | None = None
_SELECTED_CACHED: tuple[str | None] | None = None
_REFUSED_LOGGED: set[tuple[str, str]] = set()


def parse_allowed(value: Any) -> frozenset[str]:
    """Normalize a yaml/env value. Fail-closed on anything unexpected."""
    if value is None:
        return DEFAULT_ALLOWED
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
        return DEFAULT_ALLOWED
    return parse_allowed(cognify.get("allowed_backends"))


def allowed() -> frozenset[str]:
    global _CACHED
    if _CACHED is None:
        _CACHED = load_allowed()
    return _CACHED


_UNSET = object()


def reset(value: frozenset[str] | None = None, selected: Any = _UNSET) -> None:
    """Tests: set (or clear) the cached allow-list and backend selection.

    ``selected`` omitted clears the selection cache (re-read from env/yaml
    on next use); pass None or a backend name to pin it."""
    global _CACHED, _SELECTED_CACHED
    _CACHED = value
    _SELECTED_CACHED = None if selected is _UNSET else (selected,)
    _REFUSED_LOGGED.clear()


def parse_selected(value: Any) -> str | None:
    """Normalize cognify.backend. None/"" = unset (model-based routing);
    an unknown value returns INVALID, which no allow-list contains."""
    if value is None:
        return None
    name = str(value).strip().lower()
    if not name:
        return None
    if name in BACKENDS:
        return name
    logger.error(
        "cognify.backend: unknown backend %r — every cognify LLM pass is refused",
        value,
    )
    return INVALID


def load_selected(cfg: dict | None = None) -> str | None:
    env = os.environ.get("DEVBRAIN_COGNIFY_BACKEND")
    if env is not None:
        return parse_selected(env)
    if cfg is None:
        try:
            from config import load_config as _load  # factory/config.py

            cfg = _load()
        except Exception:  # noqa: BLE001
            # load_allowed() already refuses everything on an unreadable
            # config, so model-based routing here can reach no backend.
            return None
    return parse_selected((cfg.get("cognify") or {}).get("backend"))


def selected() -> str | None:
    """The explicitly selected backend (cognify.backend), or None."""
    global _SELECTED_CACHED
    if _SELECTED_CACHED is None:
        _SELECTED_CACHED = (load_selected(),)
    return _SELECTED_CACHED[0]


def fallback_enabled() -> bool:
    """Cross-backend fallback (codex -> Anthropic) only runs under
    model-based routing. An explicit cognify.backend means that backend
    and nothing else — the PHI-critical property for backend=openai."""
    return selected() is None


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


def unsupported(backend: str, pass_name: str) -> None:
    """Log (once per process) that ``pass_name`` has no path for the
    selected backend, so it does not run."""
    key = (backend, pass_name + ":unsupported")
    if key not in _REFUSED_LOGGED:
        _REFUSED_LOGGED.add(key)
        logger.warning(
            "cognify %s: cognify.backend=%r has no %s path — pass not run",
            pass_name,
            backend,
            pass_name,
        )


def backend_for_model(model: str | None) -> str:
    """The backend a model id routes to (mirrors extract._routes_to_codex).
    Ignores cognify.backend — use route() for the effective backend."""
    from cognify.extract import _routes_to_codex

    return "codex" if _routes_to_codex(model) else "anthropic"


def route(model: str | None) -> str:
    """The backend a cognify call for ``model`` goes to: the explicit
    cognify.backend when set, else the model-based routing."""
    sel = selected()
    if sel is not None:
        return sel
    return backend_for_model(model)


# ── Per-run backend chain + availability (resummarize, edges) ────────────────
#
# A pass that makes many small calls asks once per run which backends it can
# actually reach, instead of discovering a missing credential once per unit.
# Reason codes, used in run-log metadata and read by brain_doctor:
#   refused      — not in cognify.allowed_backends (or cognify.backend is
#                  invalid): a policy decision, e.g. a PHI deployment
#   no_auth      — allowed, but no credential / login / safe CODEX_HOME
#   unavailable  — allowed, but the codex binary or anthropic SDK is missing


def chain(primary_model: str | None, fallback_model: str | None) -> list[str]:
    """Backends a pass would try, in order, before availability.

    An explicit cognify.backend means exactly that backend (no fallback).
    Otherwise the primary routes by model id (codex for "codex"/gpt-*), and
    a codex primary falls back to Anthropic when ``fallback_model`` names a
    non-codex model — the order fanout and extract use."""
    sel = selected()
    if sel is not None:
        return [sel]
    primary = backend_for_model(primary_model)
    out = [primary]
    if (
        primary == "codex"
        and fallback_model
        and fallback_model.strip().lower() not in ("", "none")
        and backend_for_model(fallback_model) == "anthropic"
    ):
        out.append("anthropic")
    return out


def availability(backend: str, pass_name: str) -> tuple[str | None, str]:
    """(reason_code | None, detail). None means the backend can be called.
    Never raises; never returns secret material."""
    if not check(backend, pass_name) or backend not in BACKENDS:
        return "refused", "not in cognify.allowed_backends"
    if backend == "codex":
        from cognify import codex_home  # noqa: PLC0415
        from cognify.extract import _CODEX_BIN  # noqa: PLC0415

        ok, reason = codex_home.check()
        if not ok:
            return "no_auth", f"codex isolation: {reason}"
        if not _CODEX_BIN or not os.path.exists(_CODEX_BIN):
            return "unavailable", f"codex binary not found at {_CODEX_BIN}"
        return None, "ok"
    if backend == "anthropic":
        try:
            import anthropic  # noqa: F401, PLC0415
        except ImportError:
            return "unavailable", "anthropic SDK not installed"
        from cognify._anthropic_auth import resolve_anthropic_auth  # noqa: PLC0415

        if resolve_anthropic_auth() is None:
            return "no_auth", "no Anthropic credential in the environment"
        return None, "ok"
    # openai
    from cognify import openai_api  # noqa: PLC0415

    if not openai_api.api_key():
        return "no_auth", "no OpenAI API key"
    return None, "ok"


def usable(backend_chain: list[str], pass_name: str) -> tuple[list[str], dict[str, str]]:
    """(usable backends in order, {backend: "code: detail"} for the rest)."""
    ok: list[str] = []
    reasons: dict[str, str] = {}
    for b in backend_chain:
        code, detail = availability(b, pass_name)
        if code is None:
            ok.append(b)
        else:
            reasons[b] = f"{code}: {detail}"
    return ok, reasons


def blocked_label(reasons: dict[str, str]) -> str:
    """Run-level label when no backend is usable: "refused" only when every
    backend was refused by policy; otherwise the first non-policy code."""
    codes = [r.split(":", 1)[0] for r in reasons.values()]
    if not codes or all(c == "refused" for c in codes):
        return "refused"
    if "no_auth" in codes:
        return "no_auth"
    return next(c for c in codes if c != "refused")
