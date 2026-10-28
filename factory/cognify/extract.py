"""cognify_extract — lesson + decision extraction from ingested sessions.

This module provides:
  1. ``run_extract_pass(conn, project_id, ...)`` — the scheduled pass that
     processes all sessions ingested since the last successful extract run.
  2. ``extract_from_session(conn, session_id, project_id, ...)`` — single-
     session extraction (used by both the scheduled pass and reextract_cli).

The extraction logic (LLM call to produce structured lessons/decisions from
raw session content) lives here. factory/curator/end_session.py delegates
to this module via ``run_extract_pass`` for any scheduled extraction; the
end_session orchestration shell remains in end_session.py.

Evidence: every extracted item must carry a quote that occurs word for word
in the session text the model saw (verify_quote; whitespace normalized only).
Items without one are dropped and counted, never written.

Idempotency: an atom's identity is its content key (uuid5 over project,
kind and normalized title — content_key). A re-run of the same session is a
no-op; the same atom from another session merges into the existing row (see
_upsert_memory).

PHI constraint: LLM prompts contain session content (which may be sensitive),
but the cognify_run_log rows written by the orchestrator never receive raw
memory.content — only row counts and metadata are logged.

LLM cost ceiling: max 20 calls per pass invocation (Sonnet 4.6).

Spend tracking: each LLM call writes a row to devbrain.cognify_spend_log
(migration 029) via observability.spend.record_spend. Costs are estimates
based on hardcoded Anthropic list prices (see observability/pricing.py).

Versioned extraction: CURRENT_EXTRACTION_VERSION marks the prompt/model
version for all newly written tier='lesson' rows. Bump this constant when
the extraction prompt or model changes, then run:
    devbrain cognify-reextract --since-version=<N>
to reprocess rows produced by prior versions.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import re
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from cognify import backends, codex_home, gate, openai_api, relate, screen
from cognify.embedding import embed_text, to_vector_literal
from cognify.orchestrator import CognifyPass, PassResult, register_pass
from observability.pricing import (
    SONNET_4_6,
    compute_cost_usd,
    get_pricing,
)
from observability.spend import record_spend

logger = logging.getLogger(__name__)

# Maximum LLM calls per scheduled pass invocation.
MAX_LLM_CALLS_PER_PASS = 20

# Retry configuration for transient APIConnectionError. The 2026-05-14
# marathon-reextract incident showed that long-running cognify processes
# accumulate stale connections in httpx's internal pool; per-call retries
# with client recycle defeat that failure mode. Other exception classes
# (auth, 400, JSONDecodeError) are NOT retried — they won't improve.
_API_MAX_RETRIES = 3
_API_BACKOFF_S = (2, 5, 15)  # exponential, applied in order

# Extraction prompt/model version. Bump this integer when the extraction
# prompt or model changes, then run `devbrain cognify-reextract --since-version=N`
# to reprocess existing rows. Starts at 1 per Phase 6 design §6.
# 2: every item carries a verified quote; ids are content-derived.
CURRENT_EXTRACTION_VERSION = 2

# Default extraction model. Routes to the codex CLI backend (schema-constrained
# JSON → no json_parse failures; runs on the local ChatGPT-sub auth). Override
# with DEVBRAIN_EXTRACT_MODEL (e.g. "claude-sonnet-4-6" for the Anthropic SDK
# path, or an explicit OpenAI id like "gpt-5"). The Anthropic path is preserved
# and still used whenever a claude-* model is requested.
_EXTRACT_MODEL = os.environ.get("DEVBRAIN_EXTRACT_MODEL", "codex")

# When the primary extractor is codex and it fails with an `api` failure
# (ChatGPT usage-limit / timeout / codex error), fall back to this Anthropic
# model so extraction never stalls on a quota wall. Set DEVBRAIN_EXTRACT_FALLBACK
# to "" / "none" to disable the fallback.
_FALLBACK_MODEL = os.environ.get("DEVBRAIN_EXTRACT_FALLBACK", "claude-sonnet-5")

# Anthropic model used when cognify.backend=anthropic but the requested
# extract model is the "codex" sentinel / an OpenAI id.
_ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-5"

# ── Codex (OpenAI CLI) extraction backend ──────────────────────────────────
# When the requested model routes here (model == "codex" or an OpenAI model id
# like "gpt-5"/"o3"), extraction is delegated to the local `codex exec` CLI
# instead of the Anthropic SDK. We use codex's --output-schema (JSON-Schema-
# constrained final response) + --output-last-message, which GUARANTEES clean
# parseable JSON — eliminating the json_parse failures the Anthropic OAuth path
# (no assistant-prefill) suffers. codex auths via the local ChatGPT-subscription
# login, so there's no per-token billing and no separate API key.
_CODEX_BIN = os.environ.get("DEVBRAIN_CODEX_BIN") or shutil.which("codex") \
    or "/opt/homebrew/bin/codex"
_CODEX_TIMEOUT_S = int(os.environ.get("DEVBRAIN_CODEX_TIMEOUT_S", "240"))


def _salvage_last_json(
    *streams: str, required_keys: tuple[str, ...] = ("lessons", "decisions"),
) -> dict | None:
    """Recover the final JSON object codex printed but never wrote to -o.

    Only an object carrying at least one of ``required_keys`` counts. codex
    also prints its API errors as JSON (``ERROR: {"type":"error",...}``);
    before 2026-10 such an error object was "salvaged" as the result, read
    as zero lessons, and reported as ``empty`` — so a rejected model looked
    like sessions with nothing to extract and the Anthropic fallback never
    ran (all extraction on one Studio from 2026-09-17).

    codex-cli 0.144.x intermittently completed the model call, echoed the
    schema-valid JSON to its progress streams, then exited 1 WITHOUT writing
    the --output-schema file — 44,730 fanout items (through 2026-08-24) paid
    twice for work that was already sitting in stderr: once on the ChatGPT
    sub for the "failed" codex run, once on Anthropic for the fallback.
    Scan the streams (stdout first — codex prints the final message there)
    for the last parseable JSON object and hand the work back instead of
    discarding it. raw_decode ignores the trailing "tokens used" chatter.
    """
    decoder = json.JSONDecoder()
    for text in streams:
        if not text:
            continue
        # Walk "{" starts in order, skipping any that fall inside an object
        # already parsed — otherwise the winner would be the final NESTED
        # object (e.g. the last list element) instead of the top-level one.
        last: dict | None = None
        skip_until = -1
        for idx in [i for i, ch in enumerate(text) if ch == "{"][:400]:
            if idx < skip_until:
                continue
            try:
                obj, consumed = decoder.raw_decode(text[idx:])
            except json.JSONDecodeError:
                continue
            skip_until = idx + consumed
            if isinstance(obj, dict) and any(k in obj for k in required_keys):
                last = obj
        if last is not None:
            return last
    return None


def _cognify_yaml() -> dict:
    try:
        from config import load_config as _load  # noqa: PLC0415 — factory/config.py

        return (_load().get("cognify") or {})
    except Exception:  # noqa: BLE001 — missing/odd config: built-in defaults
        return {}


# Model passed to `codex exec` when the extract model is the "codex" sentinel.
# We must pass an EXPLICIT -m: the codex CLI's built-in default and the slugs
# a ChatGPT account accepts both rotate (the -codex models were retired
# 2026-06-02; gpt-5.4-mini and gpt-5.5 by 2026-10). A retired slug 400s with
# "model is not supported when using Codex with a ChatGPT account".
# Precedence: env DEVBRAIN_CODEX_MODEL > yaml cognify.codex_model > default.
# The account's current list is in ~/.codex/models_cache.json.
_CODEX_FALLBACK_DEFAULT_MODEL = "gpt-5.6-luna"
_CODEX_DEFAULT_MODEL = (
    os.environ.get("DEVBRAIN_CODEX_MODEL")
    or str(_cognify_yaml().get("codex_model") or "")
    or _CODEX_FALLBACK_DEFAULT_MODEL
)

# Reasoning effort for codex exec. Without it codex inherits whatever the
# host's interactive ~/.codex/config.toml says (xhigh on one Studio), which
# is slow and costly for a structured extraction and risks the exec timeout.
# env DEVBRAIN_CODEX_REASONING_EFFORT > yaml cognify.codex_reasoning_effort >
# "medium". "inherit" (or "") leaves config.toml in charge.
_CODEX_REASONING_EFFORT = (
    os.environ.get("DEVBRAIN_CODEX_REASONING_EFFORT")
    if os.environ.get("DEVBRAIN_CODEX_REASONING_EFFORT") is not None
    else str(_cognify_yaml().get("codex_reasoning_effort", "medium") or "")
)


def codex_config_args() -> list[str]:
    """Extra ``codex exec`` flags shared by extract and fanout."""
    effort = (_CODEX_REASONING_EFFORT or "").strip()
    if not effort or effort.lower() == "inherit":
        return []
    if not effort.replace("-", "").replace("_", "").isalnum():
        logger.warning("cognify codex: ignoring odd reasoning effort %r", effort)
        return []
    return ["-c", f'model_reasoning_effort="{effort}"']


def codex_error(*streams: str) -> str | None:
    """The API error codex printed (``ERROR: {...}``), as one line, with a
    hint when the model slug was rejected. None when there is none."""
    msg = None
    for text in streams:
        for line in (text or "").splitlines():
            line = line.strip()
            if not line.startswith("ERROR:"):
                continue
            body = line[len("ERROR:"):].strip()
            try:
                obj = json.loads(body)
                err = obj.get("error") if isinstance(obj, dict) else None
                msg = (err or {}).get("message") if isinstance(err, dict) else None
                msg = msg or body
            except (json.JSONDecodeError, AttributeError):
                msg = body
    if msg and "model is not supported" in msg:
        avail = codex_listed_models()
        msg += (
            " Set DEVBRAIN_CODEX_MODEL or cognify.codex_model"
            + (f" (account lists: {', '.join(avail)})" if avail else "")
            + "."
        )
    return msg[:400] if msg else None


def codex_listed_models() -> list[str]:
    """Model slugs the logged-in account lists, from codex's own catalog
    cache. Best-effort (empty when the cache is missing or changes shape)."""
    home = os.environ.get("CODEX_HOME") or str(codex_home.resolve_home())
    try:
        with open(os.path.join(home, "models_cache.json")) as fh:
            models = json.load(fh).get("models") or []
        return [
            m["slug"] for m in models
            if isinstance(m, dict) and m.get("slug") and m.get("visibility") == "list"
        ]
    except Exception:  # noqa: BLE001
        return []

# JSON Schema for the extractor's final response — same shape the Anthropic
# path parses ({lessons:[{title,content,quote}], decisions:[...]}).
_ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "content", "quote"],
    "properties": {
        "title": {"type": "string"},
        "content": {"type": "string"},
        "quote": {"type": "string"},
    },
}
_CODEX_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["lessons", "decisions"],
    "properties": {
        "lessons": {"type": "array", "items": _ITEM_SCHEMA},
        "decisions": {"type": "array", "items": _ITEM_SCHEMA},
    },
}

# Characters of session text sent to the model. The quote check runs
# against exactly this text.
MAX_PROMPT_CHARS = 200_000

# Shared by every backend's prompt. The quote is the item's evidence: it is
# checked word for word against the session text in code (see
# verify_quote) and an item without a matching quote is dropped.
_EXTRACT_INSTRUCTIONS = (
    "Extract two kinds of items:\n"
    "1. Lessons: generalizable insights or patterns worth remembering.\n"
    "2. Decisions: specific architectural or implementation choices made.\n\n"
    "Each item has:\n"
    "- title: short, <= 80 chars.\n"
    "- content: the lesson or decision in detail.\n"
    "- quote: one passage copied EXACTLY, character for character, from the "
    "session text that shows the item (one or two sentences, at most 300 "
    "chars). Copy it; do not paraphrase, shorten with ellipses, fix typos or "
    "add quotation marks around it.\n\n"
    "Only extract items the session text itself supports. An item without "
    "an exact quote from the session is discarded. If nothing is worth "
    "keeping, return empty lists."
)

# Quotes shorter than this (after whitespace normalization) do not count:
# a two-word "quote" matches almost any session.
MIN_QUOTE_CHARS = 12

# Namespace for content-derived atom ids (uuid5). Fixed forever: changing it
# changes every derived id and breaks re-run merging.
CONTENT_KEY_NAMESPACE = uuid.UUID("6f0c4a1e-3b7d-5c2a-9e41-d3b8a2f17c55")


def _fallback_configured() -> bool:
    # An explicit cognify.backend disables the cross-backend fallback: with
    # backend=openai nothing may ever go to Anthropic or codex instead.
    return bool(
        backends.fallback_enabled()
        and _FALLBACK_MODEL
        and _FALLBACK_MODEL.lower() not in ("none", "")
        and not _routes_to_codex(_FALLBACK_MODEL)
    )


def _backend_chain() -> list[str]:
    """Backends this pass would try, in order, before the allow-list."""
    primary = backends.route(_EXTRACT_MODEL)
    chain = [primary]
    if primary == "codex" and _fallback_configured():
        chain.append("anthropic")
    return chain


def _usable_backends() -> list[str]:
    """Backends this pass could use, after cognify.allowed_backends (and,
    for openai, a resolvable API key)."""
    out = []
    for b in _backend_chain():
        if not backends.is_allowed(b):
            continue
        if b == "openai" and not openai_api.api_key():
            continue
        out.append(b)
    return out


def _refused_result(detail: str) -> dict:
    return {
        "lessons": [], "decisions": [],
        "_usage": {"input_tokens": 0, "output_tokens": 0,
                   "cache_read_tokens": 0, "cache_write_tokens": 0},
        "_failure": "refused", "_failure_detail": detail,
    }


def _routes_to_codex(model: str | None) -> bool:
    """True when the requested model should be handled by the codex CLI."""
    if not model:
        return False
    m = model.lower()
    return m == "codex" or m.startswith(("gpt-", "gpt5", "o3", "o4", "codex-"))


# ── Quote check + content-derived identity ─────────────────────────────────


def normalize_ws(text: str) -> str:
    """Collapse every whitespace run to one space and strip the ends. The
    only normalization the quote check applies."""
    return " ".join((text or "").split())


def verify_quote(quote: Any, source_normalized: str) -> str | None:
    """None when ``quote`` occurs word for word in the (already
    whitespace-normalized) source; otherwise the reason it fails:

      no_quote         missing, not a string, or shorter than MIN_QUOTE_CHARS
      quote_not_found  not a verbatim substring of the source

    Case, punctuation and quotation marks must match exactly; only
    whitespace is normalized."""
    if not isinstance(quote, str):
        return "no_quote"
    q = normalize_ws(quote)
    if len(q) < MIN_QUOTE_CHARS:
        return "no_quote"
    if q not in source_normalized:
        return "quote_not_found"
    return None


_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


def normalize_claim(text: str) -> str:
    """Normalization for content keys: NFKC, case-folded, every run of
    non-alphanumerics collapsed to one space. "Use UUID5 ids!" and
    "use  uuid5 IDs" map to the same key."""
    t = unicodedata.normalize("NFKC", text or "").casefold()
    return _NON_WORD.sub(" ", t).strip()


def content_key(project_id: Any, db_kind: str, title: str, content: str) -> UUID:
    """Content-derived identity of an extracted atom: uuid5 over project,
    storage kind and the normalized title (the content when there is no
    title). The same lesson extracted again — from a re-run or from another
    session of the same project — gets the same key, so it merges into the
    existing row instead of adding a duplicate."""
    claim = normalize_claim(title) or normalize_claim(content)
    return uuid.uuid5(CONTENT_KEY_NAMESPACE, f"{project_id}\x1f{db_kind}\x1f{claim}")


@dataclass
class ExtractResult:
    """Result of extracting lessons/decisions from a single session.

    `lessons_created` is the LLM-facing nomenclature — the prompt asks
    the model to produce "lessons" and we count what it returned. In
    DB storage these rows land with `kind='pattern'` (the storage-layer
    naming); see `_upsert_memory(..., kind='pattern', ...)` below. The
    `patterns_created` attribute is an alias for the same int, exposed
    so SQL-side code can use the storage name without remembering the
    LLM-side rename.
    """

    session_id: str
    lessons_created: int = 0
    decisions_created: int = 0
    skipped_duplicates: int = 0
    llm_calls: int = 0
    memory_ids: list[str] = field(default_factory=list)
    failure: str | None = None  # None | "api" | "json_parse" | "empty" | "refused"
    # Items the model returned (lessons + decisions), before any check.
    items_returned: int = 0
    # Per-item outcomes: no_quote / quote_not_found / empty_item (dropped
    # before any write) and created / duplicate / merged / relation_same
    # (see _upsert_memory).
    item_outcomes: dict[str, int] = field(default_factory=dict)
    # When codex failed and the Anthropic fallback ran: "used" or
    # "failed:<kind>". None when no fallback was attempted.
    fallback: str | None = None

    @property
    def patterns_created(self) -> int:
        """Alias for lessons_created — matches DB storage kind ('pattern')."""
        return self.lessons_created

    @property
    def zero_yield_reason(self) -> str | None:
        """Why this unit produced no new row; None when it produced one.

          <failure>          api / json_parse / empty / refused
          no_source          no source rows (or empty text): no LLM call
          all_dropped        every item failed the quote check
          all_known          every kept item already existed (same session
                             re-run, content-key merge, or a "same" verdict)
        """
        if self.lessons_created or self.decisions_created:
            return None
        if self.failure:
            return self.failure
        if self.items_returned == 0:
            return "no_source" if self.llm_calls == 0 else "empty"
        dropped = sum(
            self.item_outcomes.get(k, 0)
            for k in ("no_quote", "quote_not_found", "empty_item")
        )
        if dropped >= self.items_returned:
            return "all_dropped"
        return "all_known"


@register_pass
class ExtractPass(CognifyPass):
    """cognify_extract: lesson/decision extraction from recent sessions.

    Hourly pass via launchd. Up to 20 LLM calls per run.
    """

    pass_name = "extract"

    def run(
        self,
        conn: Any,
        project_id: Any,
        *,
        dry_run: bool = False,
        max_llm_calls: int | None = None,
    ) -> PassResult:
        """Extract lessons/decisions from sessions ingested since last pass.

        Args:
            conn: psycopg2 connection.
            project_id: UUID. Required for extract (LLM pass; project-scoped).
            dry_run: if True, compute candidate sessions without extracting.
            max_llm_calls: per-pass ceiling on LLM calls. When None (default),
                uses MAX_LLM_CALLS_PER_PASS=20.

        Returns:
            PassResult with row counts.
        """
        if project_id is None:
            raise ValueError(
                "cognify_extract requires a project_id "
                "(it's an LLM-cost pass; always project-scoped)"
            )

        cap = max_llm_calls if max_llm_calls is not None else MAX_LLM_CALLS_PER_PASS

        if not dry_run and not _usable_backends():
            # Nothing this deployment allows can run. The row is marked
            # "refused" so it moves neither the watermark nor the backlog:
            # once a backend is allowed, nothing ingested meanwhile is lost.
            for b in _backend_chain():
                backends.check(b, "extract")
            return PassResult(
                rows_processed=0,
                llm_calls=0,
                metadata={"pass": "extract", "refused": True,
                          "allowed_backends": sorted(backends.allowed())},
            )

        since = _last_successful_run(conn, "extract", project_id)
        # Units the last run could not finish (LLM cap reached, or an api
        # failure) come first; without this they fell behind the watermark
        # and were never extracted.
        tries = _last_backlog(conn, "extract", project_id)
        candidate_sessions = list(tries)
        candidate_sessions += [
            s for s in _sessions_since(conn, project_id, since) if s not in tries
        ]

        if dry_run:
            return PassResult(
                rows_processed=0,
                llm_calls=0,
                metadata={
                    "pass": "extract",
                    "dry_run_candidate_sessions": len(candidate_sessions),
                    "dry_run_backlog_sessions": len(tries),
                    "since": since.isoformat() if since else None,
                },
            )

        total_lessons = 0
        total_decisions = 0
        total_llm = 0
        sessions_processed = 0
        failure_counts: dict[str, int] = {}
        # Why units produced nothing (see ExtractResult.zero_yield_reason),
        # per-item outcomes, and the codex→Anthropic fallback's results.
        # Ids and counts only: no memory text reaches the run log.
        zero_yield: dict[str, int] = {}
        zero_yield_ids: list[dict] = []
        items: dict[str, int] = {}
        fallback: dict[str, int] = {}
        stopped: str | None = None

        # Cheap pre-LLM gate (cognify.extract_gate; off by default). In
        # dry_run it only logs; in "on" skipped units are dropped and
        # downgraded units go to the back of the queue, so they only use
        # LLM budget that is left after the others.
        gate_cfg = gate.get_config()
        gate_log = gate.GateLog(gate_cfg.mode) if gate_cfg.enabled else None
        deferred: list[str] = []
        evaluated: set[str] = set()
        done: set[str] = set()
        gate_skipped: set[str] = set()
        retry: list[str] = []

        def _queue():
            yield from candidate_sessions
            yield from deferred

        for session_id in _queue():
            if total_llm >= cap:
                logger.info(
                    "cognify_extract: LLM cap (%d) reached, deferring remaining sessions",
                    cap,
                )
                break
            if gate_log is not None and session_id not in evaluated:
                evaluated.add(session_id)
                decision = gate.evaluate(conn, session_id, project_id, gate_cfg)
                gate_log.add(decision)
                if gate_cfg.applies and decision.action == "skip":
                    gate_skipped.add(session_id)
                    continue
                if gate_cfg.applies and decision.action == "downgrade":
                    deferred.append(session_id)
                    continue
            result = extract_from_session(
                conn,
                session_id,
                project_id,
                max_llm_calls=cap - total_llm,
            )
            total_lessons += result.lessons_created
            total_decisions += result.decisions_created
            total_llm += result.llm_calls
            sessions_processed += 1
            if result.failure != "refused":
                # A refused unit made no call: carry it (tries unchanged).
                done.add(session_id)
            if result.failure:
                failure_counts[result.failure] = failure_counts.get(result.failure, 0) + 1
            if result.failure == "api":
                retry.append(session_id)
            _tally(items, result.item_outcomes)
            if result.items_returned:
                _tally(items, {"returned": result.items_returned})
            # Items dropped by the quote check count as failures too, so a
            # model that stops quoting shows up where the other failures do.
            _tally(failure_counts, {
                k: v for k, v in result.item_outcomes.items()
                if k in ("no_quote", "quote_not_found", "empty_item")
            })
            if result.fallback:
                _tally(fallback, {result.fallback: 1})
            reason = result.zero_yield_reason
            if reason:
                _tally(zero_yield, {reason: 1})
                if len(zero_yield_ids) < _MAX_ZERO_YIELD_IDS:
                    zero_yield_ids.append({"id": session_id, "reason": reason})
            if result.failure == "refused":
                # Refusals are deployment-wide (backend not allowed, no
                # credential, unsafe codex home): every later unit would be
                # refused too. Stop; the rest is carried to the next run.
                stopped = "refused"
                logger.warning(
                    "cognify_extract: backend refused; carrying the remaining "
                    "units to the next run"
                )
                break

        backlog = _next_backlog(
            candidate_sessions + deferred, done, gate_skipped, retry, tries
        )
        if backlog:
            logger.info(
                "cognify_extract: %d session(s) carried to the next run", len(backlog)
            )

        rows = total_lessons + total_decisions
        return PassResult(
            rows_processed=rows,
            llm_calls=total_llm,
            metadata={
                "pass": "extract",
                "sessions_processed": sessions_processed,
                # LLM-side naming (what the model produces); DB stores these
                # under `kind='pattern'`, so `patterns_created` is an alias.
                "lessons_created": total_lessons,
                "patterns_created": total_lessons,
                "decisions_created": total_decisions,
                # A4: surface per-failure-kind counts so silent JSON parse
                # errors and API failures are visible in the run log.
                # Includes per-item quote-check drops (no_quote,
                # quote_not_found, empty_item).
                "failure_counts": failure_counts,
                # Units that wrote nothing, by reason, plus the first ids
                # (for `devbrain cognify-reextract --session=<id>`).
                "zero_yield": zero_yield,
                "zero_yield_ids": zero_yield_ids,
                # returned / created / duplicate / merged / relation_same /
                # no_quote / quote_not_found / empty_item.
                "items": items,
                "fallback": fallback,
                "stopped": stopped,
                "since": since.isoformat() if since else None,
                "gate": gate_log.summary() if gate_log is not None else None,
                # Read back by the next run (_last_backlog). Ids only.
                "backlog": backlog,
            },
        )


_MAX_ZERO_YIELD_IDS = 50


def _tally(into: dict[str, int], add: dict[str, int]) -> None:
    for k, v in add.items():
        if v:
            into[k] = into.get(k, 0) + v


def run_extract_pass(
    conn: Any,
    project_id: Any,
    *,
    dry_run: bool = False,
) -> PassResult:
    """Convenience wrapper used by end_session.py and the reextract CLI.

    Delegates to ExtractPass.run().
    """
    return ExtractPass().run(conn, project_id, dry_run=dry_run)


def extract_from_session(
    conn: Any,
    session_id: str,
    project_id: Any,
    *,
    max_llm_calls: int = MAX_LLM_CALLS_PER_PASS,
    reextract: bool = False,
    model: str | None = None,
) -> ExtractResult:
    """Extract lessons and decisions from a single session's memory chunks.

    Args:
        conn: psycopg2 connection.
        session_id: provenance_id of the session to extract from.
        project_id: UUID. Used to scope the memory INSERT and validate
            isolation.
        max_llm_calls: upper bound on LLM calls this function may make.
        reextract: if True, archive existing lessons/decisions for this
            session before re-extracting (for reextract_cli).
        model: optional Anthropic model id override (e.g.
            "claude-opus-4-7"). Defaults to _EXTRACT_MODEL (Sonnet 4.6).
            Lets callers run a more capable model on sessions that
            failed json_parse with Sonnet — at higher cost per call.

    Returns:
        ExtractResult with counts.
    """
    if reextract:
        _archive_prior_extracts(conn, session_id, project_id)

    # Fetch raw chunks for this session.
    chunks = _load_session_chunks(conn, session_id, project_id)
    if not chunks:
        logger.debug(
            "cognify_extract: session %s has no chunks to extract from",
            session_id,
        )
        return ExtractResult(session_id=session_id)

    # Build combined content for the LLM. The model sees (and must quote
    # from) only the first MAX_PROMPT_CHARS characters.
    combined = _combine_chunks(chunks)[:MAX_PROMPT_CHARS]
    if not combined.strip():
        return ExtractResult(session_id=session_id)
    fallback_outcome: str | None = None

    # Call LLM for extraction. An explicit cognify.backend wins and gets no
    # fallback. Otherwise route to the codex CLI (schema-constrained,
    # reliable JSON) when the requested model is "codex"/an OpenAI id, else
    # the Anthropic SDK path.
    effective_model = model or _EXTRACT_MODEL
    selected = backends.selected()
    if selected == "openai":
        # Explicit OpenAI platform backend: no fallback of any kind.
        effective_model = openai_api.model_for(effective_model)
        extracted = _openai_extract(combined, model=effective_model)
    elif selected == "anthropic":
        if _routes_to_codex(effective_model):
            effective_model = _ANTHROPIC_DEFAULT_MODEL
        extracted = _llm_extract(
            combined, max_llm_calls=max_llm_calls, model=effective_model,
        )
    elif selected is not None and selected != "codex":
        backends.check(selected, "extract")  # INVALID: logs the refusal
        extracted = _refused_result(f"cognify.backend {selected!r} not usable")
    elif selected == "codex" or _routes_to_codex(effective_model):
        if not _routes_to_codex(effective_model):
            effective_model = "codex"
        extracted = _codex_extract(combined, model=effective_model)
        # Fall back to Anthropic when codex hits a usage-limit / timeout /
        # error ("api"), so a ChatGPT quota wall can't stall extraction. We
        # only fall back on "api" (not "empty"): an empty result is a valid
        # "this session had nothing to extract", not a failure to retry.
        # A codex refusal (cognify.allowed_backends, or no safe codex home)
        # goes straight to the fallback too, when that backend is allowed.
        if (
            extracted.get("_failure") in ("api", "refused")
            and _fallback_configured()
        ):
            logger.warning(
                "cognify_extract: codex failed (%s); falling back to %s",
                (extracted.get("_failure_detail") or "")[:160], _FALLBACK_MODEL,
            )
            fallback = _llm_extract(
                combined, max_llm_calls=max_llm_calls, model=_FALLBACK_MODEL,
            )
            fallback_outcome = (
                "used" if fallback.get("_failure") in (None, "empty")
                else f"failed:{fallback['_failure']}"
            )
            if fallback.get("_failure") not in (None, "empty"):
                logger.warning(
                    "cognify_extract: fallback %s failed too (%s: %s)",
                    _FALLBACK_MODEL, fallback.get("_failure"),
                    (fallback.get("_failure_detail") or "")[:160],
                )
            # Use the fallback only if it actually succeeded; otherwise keep
            # codex's failure so telemetry reflects the primary backend
            # (unless codex never ran: then report the fallback's outcome).
            # "empty" is a completed call that found nothing: accept it (and
            # its spend) rather than retrying the unit as a codex failure.
            if fallback.get("_failure") in (None, "empty"):
                extracted = fallback
                effective_model = _FALLBACK_MODEL  # spend block bills the right model
            elif extracted.get("_failure") == "refused":
                extracted = fallback
    else:
        extracted = _llm_extract(
            combined, max_llm_calls=max_llm_calls, model=effective_model,
        )
    # One call per session for v1; a refusal made no call.
    llm_calls = 0 if extracted.get("_failure") == "refused" else 1

    # Record spend for this LLM call. Use the pricing registry to
    # look up the right rates; fall back to SONNET_4_6 if the caller
    # passed an unfamiliar model so we still bill something rather
    # than crash.
    usage = extracted.get("_usage", {})
    if any(usage.get(k, 0) for k in ("input_tokens", "output_tokens")):
        pricing = get_pricing(effective_model) or SONNET_4_6
        cost = compute_cost_usd(
            pricing,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_tokens", 0),
            cache_write_tokens=usage.get("cache_write_tokens", 0),
        )
        record_spend(
            conn,
            project_id=project_id,
            pass_name="extract",
            model=effective_model,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_tokens", 0),
            cache_write_tokens=usage.get("cache_write_tokens", 0),
            cost_usd=cost,
        )

    created = {"lesson": 0, "decision": 0}
    skipped = 0
    memory_ids: list[str] = []
    outcomes: dict[str, int] = {}
    returned = 0
    # The quote check runs against the exact text the model saw, with
    # whitespace runs collapsed on both sides.
    source_normalized = normalize_ws(combined)

    for kind, key in (("lesson", "lessons"), ("decision", "decisions")):
        items = extracted.get(key) or []
        if not isinstance(items, list):
            items = []
        for item in items:
            returned += 1
            if not isinstance(item, dict):
                _tally(outcomes, {"empty_item": 1})
                continue
            title = str(item.get("title") or "")
            content = str(item.get("content") or "")
            if not normalize_claim(title) and not normalize_claim(content):
                _tally(outcomes, {"empty_item": 1})
                continue
            # No quote, no row: an item the session text does not contain
            # word for word is dropped before anything is embedded or
            # written.
            quote_failure = verify_quote(item.get("quote"), source_normalized)
            if quote_failure:
                _tally(outcomes, {quote_failure: 1})
                continue
            report: dict = {}
            mid, was_new = _upsert_memory(
                conn,
                project_id=project_id,
                kind=kind,
                title=title,
                content=content,
                session_id=session_id,
                reextract=reextract,
                reextract_meta=item.get("reextracted_from"),
                quote=normalize_ws(item["quote"]),
                report=report,
            )
            _tally(outcomes, {
                report.get("outcome") or ("created" if was_new else "duplicate"): 1
            })
            if was_new:
                created[kind] += 1
                memory_ids.append(str(mid))
            else:
                skipped += 1

    if outcomes.get("no_quote") or outcomes.get("quote_not_found"):
        logger.info(
            "cognify_extract: session %s: %d of %d item(s) dropped by the quote "
            "check (no_quote=%d quote_not_found=%d)",
            session_id,
            outcomes.get("no_quote", 0) + outcomes.get("quote_not_found", 0),
            returned, outcomes.get("no_quote", 0), outcomes.get("quote_not_found", 0),
        )

    return ExtractResult(
        session_id=session_id,
        lessons_created=created["lesson"],
        decisions_created=created["decision"],
        skipped_duplicates=skipped,
        llm_calls=llm_calls,
        memory_ids=memory_ids,
        failure=extracted.get("_failure"),
        items_returned=returned,
        item_outcomes=outcomes,
        fallback=fallback_outcome,
    )


# ── Private helpers ───────────────────────────────────────────────────────────


# `devbrain cognify --pass=extract --dry-run` writes a completed run-log
# row too. It extracts nothing, so it must not move the watermark (it used
# to: a dry run silently dropped every unit ingested since the last real
# run) or replace the backlog.
#
# A run refused by cognify.allowed_backends extracted nothing either.
_NOT_DRY_RUN = (
    "coalesce(metadata ->> 'dry_run', 'false') <> 'true' "
    "AND coalesce(metadata ->> 'refused', 'false') <> 'true'"
)


def _last_successful_run(conn: Any, pass_name: str, project_id: Any):
    """Return the started_at of the most recent *completed* successful run, or None.

    Must require `completed_at IS NOT NULL`, not just `error IS NULL`. The
    orchestrator inserts+commits the run-log row at pass START (error and
    completed_at both NULL), so an `error IS NULL` filter alone matches the
    CURRENT in-progress run — extract would read its own row, get
    since=now, and process zero sessions every time (observed 2026-06-26:
    extract had been a silent no-op, rows_processed=0 every hour). It also
    matches a crashed run that never recorded its error, which would
    advance the watermark past sessions it never extracted (silent loss).
    Requiring a non-NULL completed_at restricts the watermark to runs that
    actually finished cleanly; re-extraction is idempotent (ON CONFLICT),
    so a slightly-too-old watermark only costs harmless reprocessing.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT started_at FROM devbrain.cognify_run_log "
            "WHERE pass_name = %s AND project_id = %s "
            "  AND completed_at IS NOT NULL AND error IS NULL "
            f"  AND {_NOT_DRY_RUN} "
            "ORDER BY started_at DESC LIMIT 1",
            (pass_name, project_id),
        )
        row = cur.fetchone()
    return row[0] if row else None


# Backlog carried between extract runs (see ExtractPass.run).
MAX_BACKLOG = 5000
MAX_API_TRIES = 3


def _last_backlog(conn: Any, pass_name: str, project_id: Any) -> dict[str, int]:
    """{unit_id: api_tries} the last completed run left unfinished. Read from
    the same run-log row _last_successful_run uses."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT metadata -> 'backlog' FROM devbrain.cognify_run_log "
            "WHERE pass_name = %s AND project_id = %s "
            "  AND completed_at IS NOT NULL AND error IS NULL "
            f"  AND {_NOT_DRY_RUN} "
            "ORDER BY started_at DESC LIMIT 1",
            (pass_name, project_id),
        )
        row = cur.fetchone()
    out: dict[str, int] = {}
    for item in (row[0] if row and isinstance(row[0], list) else []):
        if isinstance(item, dict) and item.get("id"):
            out[str(item["id"])] = int(item.get("tries") or 0)
    return out


def _next_backlog(
    queue: list[str],
    done: set[str],
    gate_skipped: set[str],
    retry: list[str],
    tries: dict[str, int],
) -> list[dict]:
    """Units to carry into the next run: everything queued but not reached
    (cap) plus api failures, each failure counting one try; a unit that
    failed MAX_API_TRIES times is dropped with a warning."""
    out: list[dict] = []
    seen: set[str] = set()
    for s in queue:
        if s in seen or s in done or s in gate_skipped:
            continue
        seen.add(s)
        out.append({"id": s, "tries": tries.get(s, 0)})
    for s in retry:
        if s in seen:
            continue
        seen.add(s)
        n = tries.get(s, 0) + 1
        if n >= MAX_API_TRIES:
            logger.warning(
                "cognify_extract: giving up on %s after %d api failures", s, n
            )
            continue
        out.append({"id": s, "tries": n})
    if len(out) > MAX_BACKLOG:
        logger.warning(
            "cognify_extract: backlog %d > %d; oldest %d dropped",
            len(out), MAX_BACKLOG, len(out) - MAX_BACKLOG,
        )
        out = out[len(out) - MAX_BACKLOG:]
    return out


def _sessions_since(conn: Any, project_id: Any, since) -> list[str]:
    """Return provenance_ids (as strings) of sessions with chunks ingested after ``since``.

    If ``since`` is None, returns all sessions for the project.
    Sessions are ordered oldest-first so re-runs converge in order.

    Only SOURCE rows count (``tier <> 'lesson'``). Extracted atoms carry the
    unit's provenance_id and are created after the run's watermark, so
    counting them made every extracted unit a candidate again on the next
    run: one 2.9k-char session summary was re-extracted in 229 separate
    hours into 2,628 atoms (2026-08). _load_session_chunks already excludes
    lesson rows from the input, so they never carried anything to extract.
    """
    if since is None:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT provenance_id::text FROM devbrain.memory "
                "WHERE project_id = %s "
                "  AND provenance_id IS NOT NULL "
                "  AND archived_at IS NULL "
                "  AND tier <> 'lesson' "
                "ORDER BY provenance_id::text",
                (project_id,),
            )
            return [r[0] for r in cur.fetchall()]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT provenance_id::text FROM devbrain.memory "
            "WHERE project_id = %s "
            "  AND provenance_id IS NOT NULL "
            "  AND archived_at IS NULL "
            "  AND tier <> 'lesson' "
            "  AND created_at > %s "
            "ORDER BY provenance_id::text",
            (project_id, since),
        )
        return [r[0] for r in cur.fetchall()]


def _load_session_chunks(conn: Any, session_id: str, project_id: Any) -> list[dict]:
    """Load raw memory chunks for this session that aren't already extracts.

    We exclude tier='lesson' rows to avoid feeding the LLM its own prior
    extraction outputs.
    """
    flags_col = ", screen_flags" if screen.has_flag_column(conn) else ""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT id, kind, title, content{flags_col} FROM devbrain.memory "
            "WHERE provenance_id = %s::uuid "
            "  AND project_id = %s "
            "  AND archived_at IS NULL "
            "  AND tier != 'lesson' "
            "ORDER BY created_at",
            (session_id, project_id),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _combine_chunks(chunks: list[dict]) -> str:
    """Combine chunks into a single text block for LLM input.

    Segments flagged by injection screening (cognify/screen.py) are
    replaced by a placeholder: text that tries to instruct the model must
    not reach the extraction prompt."""
    parts = []
    withheld = 0
    for c in chunks:
        title = c.get("title") or ""
        content, flags = screen.withhold(c.get("content") or "", c.get("screen_flags"))
        withheld += bool(flags)
        if title:
            parts.append(f"## {title}\n{content}")
        else:
            parts.append(content)
    combined = "\n\n".join(parts)
    # Screen the exact prompt text too: a payload split across two rows, or
    # between a title and its content, only exists once they are joined.
    combined, joined_flags = screen.withhold(combined)
    if withheld or joined_flags:
        logger.info(
            "cognify_extract: injection screening withheld text (%d row(s); joined: %s)",
            withheld, ",".join(joined_flags) or "-",
        )
    return combined


def _codex_extract(content: str, *, model: str | None = None) -> dict:
    """Extract lessons/decisions via the local `codex exec` CLI.

    Returns the same dict shape as `_llm_extract`:
      {"lessons": [...], "decisions": [...], "_usage": {...}, "_failure": ...}

    Uses codex's --output-schema (constrains the model's FINAL response to our
    JSON schema) + --output-last-message (captures only that final answer, no
    agent chatter), run in a read-only, ephemeral, non-git sandbox so it never
    touches the filesystem or waits on approvals. stdin is closed (codex exec
    otherwise blocks reading stdin even when a prompt arg is given).

    codex bills against the local ChatGPT-subscription login → no token spend
    to record, so _usage is zeroed (the caller's spend block then no-ops).
    """
    empty_usage = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
    }
    if not backends.check("codex", "extract"):
        return _refused_result("codex not in cognify.allowed_backends")
    home = codex_home.resolve_home()
    ok, reason = codex_home.check(home)
    if not ok:
        logger.error("cognify_extract(codex): refusing to run codex: %s", reason)
        return _refused_result(f"codex isolation: {reason}")
    if not _CODEX_BIN or not os.path.exists(_CODEX_BIN):
        logger.warning("cognify_extract(codex): codex binary not found at %s", _CODEX_BIN)
        return {"lessons": [], "decisions": [], "_usage": empty_usage,
                "_failure": "api", "_failure_detail": "codex binary missing"}

    prompt = (
        "You are a structured knowledge extractor. From the dev session below, "
        + _EXTRACT_INSTRUCTIONS
        + "\n\nReturn ONLY the structured object matching the provided schema."
        "\n\nSession:\n\n" + content[:MAX_PROMPT_CHARS]
    )

    with tempfile.TemporaryDirectory(prefix="cognify-codex-") as wd:
        schema_path = os.path.join(wd, "schema.json")
        out_path = os.path.join(wd, "out.json")
        with open(schema_path, "w") as fh:
            json.dump(_CODEX_OUTPUT_SCHEMA, fh)

        cmd = [
            _CODEX_BIN, "exec",
            "--sandbox", "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "-C", wd,
            "--output-schema", schema_path,
            "-o", out_path,
        ]
        # Always pass an explicit -m. A caller-supplied OpenAI id wins;
        # otherwise the "codex" sentinel maps to _CODEX_DEFAULT_MODEL rather
        # than codex's built-in default (a retired -codex model that 400s for
        # ChatGPT-account auth — see _CODEX_DEFAULT_MODEL).
        codex_model = model if (model and model.lower() != "codex") else _CODEX_DEFAULT_MODEL
        cmd += ["-m", codex_model]
        cmd += codex_config_args()
        cmd.append(prompt)

        try:
            proc = subprocess.run(
                cmd, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=_CODEX_TIMEOUT_S,
                env=codex_home.env_for(home),
            )
        except subprocess.TimeoutExpired:
            return {"lessons": [], "decisions": [], "_usage": empty_usage,
                    "_failure": "api", "_failure_detail": "codex exec timeout"}

        result = None
        if not os.path.exists(out_path):
            # The -o file is missing but the run may still have succeeded —
            # see _salvage_last_json. Only give up if there is no JSON to
            # recover from the streams either.
            result = _salvage_last_json(proc.stdout or "", proc.stderr or "")
            if result is None:
                err = codex_error(proc.stdout or "", proc.stderr or "")
                if err:
                    logger.error("cognify_extract(codex): codex -m %s failed: %s", codex_model, err)
                    detail = f"codex error (rc={proc.returncode}): {err}"
                else:
                    detail = (
                        f"codex produced no output (rc={proc.returncode}): "
                        f"{(proc.stderr or '')[-400:]}"
                    )
                return {"lessons": [], "decisions": [], "_usage": empty_usage,
                        "_failure": "api", "_failure_detail": detail}
        if result is None:
            try:
                with open(out_path) as fh:
                    result = json.load(fh)
            except (json.JSONDecodeError, OSError) as exc:
                return {"lessons": [], "decisions": [], "_usage": empty_usage,
                        "_failure": "json_parse", "_failure_detail": str(exc)[:300]}

    if not isinstance(result, dict):
        return {"lessons": [], "decisions": [], "_usage": empty_usage,
                "_failure": "json_parse", "_failure_detail": "non-object output"}
    result.setdefault("lessons", [])
    result.setdefault("decisions", [])
    result["_usage"] = empty_usage
    if not result.get("lessons") and not result.get("decisions"):
        result["_failure"] = "empty"
    return result


def _openai_extract(content: str, *, model: str | None = None) -> dict:
    """Extract lessons/decisions via the OpenAI platform API.

    Same dict shape as `_llm_extract` / `_codex_extract`. Strict
    json_schema output (the codex schema). A missing key is reported as
    ``refused`` so the session stays eligible instead of being marked done.
    """
    res = openai_api.complete(
        "You are a structured knowledge extractor. From the dev session the "
        "user provides, " + _EXTRACT_INSTRUCTIONS
        + "\n\nReturn ONLY the structured object matching the provided schema.",
        "Session:\n\n" + content[:MAX_PROMPT_CHARS],
        pass_name="extract",
        model=model,
        json_schema=_CODEX_OUTPUT_SCHEMA,
        schema_name="extraction",
        max_output_tokens=32_000,
    )
    if res.failure in ("refused", "no_auth"):
        return _refused_result(res.detail or res.failure)
    out: dict = {"lessons": [], "decisions": []}
    if res.parsed is not None:
        out.update(res.parsed)
        out.setdefault("lessons", [])
        out.setdefault("decisions", [])
    out["_usage"] = res.usage
    if res.failure:
        out["_failure"], out["_failure_detail"] = res.failure, res.detail
    elif not out.get("lessons") and not out.get("decisions"):
        out["_failure"] = "empty"
    return out


def _llm_extract(
    content: str,
    *,
    max_llm_calls: int = 1,
    model: str = _EXTRACT_MODEL,
) -> dict:
    """Call the LLM to extract structured lessons and decisions.

    Returns a dict with keys:
      - "lessons": list of {title, content}
      - "decisions": list of {title, content}
      - "_usage": dict with token usage fields for spend tracking
          (input_tokens, output_tokens, cache_read_tokens, cache_write_tokens).
          Always present; all counts are 0 if the LLM was not called.

    v1 implementation: uses the Anthropic SDK with prompt caching.
    If the SDK is not available (e.g. CI without API key), returns empty
    lists so the pass degrades gracefully.
    """
    _empty_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    if not backends.check("anthropic", "extract"):
        return _refused_result("anthropic not in cognify.allowed_backends")
    # No SDK or no credential means no call was made. Report it as
    # "refused" (the unit stays eligible) — never as an empty success.
    # Until 2026-10 both returned empty lists with no _failure: when codex
    # failed and the fallback had no credential, every unit was counted as
    # one LLM call that legitimately found nothing, marked done, and lost
    # (trifekta-ai, 2026-10-21 → 10-28: 20 calls, 0 rows, empty
    # failure_counts per run).
    try:
        import anthropic  # noqa: PLC0415
    except ImportError:
        logger.warning("cognify_extract: anthropic SDK not available; skipping LLM")
        return _refused_result("anthropic SDK not installed")

    auth_kwargs = _resolve_auth()
    if auth_kwargs is None:
        logger.warning(
            "cognify_extract: no Anthropic credential configured "
            "(ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_AUTH_TOKEN); "
            "skipping LLM"
        )
        return _refused_result("no Anthropic credential")

    client = anthropic.Anthropic(**auth_kwargs)

    system_prompt = (
        "You are a structured knowledge extractor. Given raw session content, "
        + _EXTRACT_INSTRUCTIONS
        + "\n\nReturn JSON with keys 'lessons' and 'decisions', each a list of "
        "objects with 'title', 'content' and 'quote'. Return only the JSON, "
        "no preamble."
    )

    # OAuth path requires the Claude Code SDK fingerprint as the first
    # system block — without it /v1/messages 429s on subscription tokens.
    # Console API key path returns None and is unaffected.
    from cognify._anthropic_auth import claude_code_system_prefix
    system_blocks: list[dict[str, Any]] = []
    oauth_prefix = claude_code_system_prefix()
    if oauth_prefix:
        system_blocks.append({"type": "text", "text": oauth_prefix})
    system_blocks.append(
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }
    )

    # max_tokens=16000: stays under the SDK's ~10-min non-streaming timeout
    # guard while giving room for rich JSON output on long sessions. Sonnet
    # 4.6 supports 64K output but anything above ~16K requires streaming.
    #
    # Input cap of 200_000 chars (~50K tokens) — Sonnet 4.6 has a 1M context
    # window, but real session_summaries top out well below 200K chars. The
    # previous 8K cap silently discarded 95%+ of any non-trivial summary.
    # Make the LLM call. Distinguish three failure modes so callers can
    # tell whether a session produced 0 atoms because:
    #   _failure="api"        the API call itself errored (network/auth/429)
    #   _failure="json_parse" the model returned non-JSON output
    #   _failure="empty"      the model returned valid JSON but with empty lists
    # All three currently degrade gracefully (return empty lists); the
    # _failure label is for telemetry/diagnostics so we can spot drift.
    #
    # APIConnectionError gets per-session retry-with-backoff because we
    # observed (2026-05-14 incident) that long-running cognify processes
    # accumulate stale connections in the SDK's internal httpx pool;
    # marathon `cognify-reextract --all` runs saw the failure rate climb
    # to ~90% Connection error in the last hours of an 18h run. Recycling
    # the client (close+new) on each retry forces a fresh httpx pool and
    # defeats that failure mode. Other exception types are NOT retried —
    # auth errors, 400s, JSONDecodeErrors won't get better from waiting.
    response = None
    api_error: Exception | None = None
    for attempt in range(_API_MAX_RETRIES):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=16000,
                system=system_blocks,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "Extract lessons and decisions from this session. "
                            "Respond with ONLY a single JSON object — no preamble, "
                            "no explanatory text, no markdown code fences. The "
                            "first character of your response must be `{` and the "
                            "last character must be `}`. Schema: "
                            '{"lessons": [{"title": str, "content": str, "quote": str}], '
                            '"decisions": [{"title": str, "content": str, "quote": str}]}.\n\n'
                            "Session:\n\n"
                            f"{content[:MAX_PROMPT_CHARS]}"
                        ),
                    },
                    # IMPORTANT: do NOT add an assistant prefill here.
                    # The Claude OAuth (Max subscription / Claude Code SDK)
                    # auth path does not support assistant message prefill
                    # and returns HTTP 400 "This model does not support
                    # assistant message prefill. The conversation must end
                    # with a user message." Confirmed via the failing
                    # llm-smoke run on PR #151 against the OAuth path
                    # (2026-05-18). The retry fix relies instead on a
                    # strengthened user prompt + the _parse_json_with_
                    # fallbacks helper below.
                ],
            )
            break
        except anthropic.APIConnectionError as exc:
            api_error = exc
            if attempt + 1 < _API_MAX_RETRIES:
                backoff = _API_BACKOFF_S[min(attempt, len(_API_BACKOFF_S) - 1)]
                logger.warning(
                    "cognify_extract: APIConnectionError (attempt %d/%d): %s; "
                    "recycling client + retrying in %ds",
                    attempt + 1, _API_MAX_RETRIES, exc, backoff,
                )
                # Drop the stale client + connection pool, then re-create.
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(backoff)
                client = anthropic.Anthropic(**auth_kwargs)
                continue
            # Exhausted retries — fall through to error return below.
        except Exception as exc:  # noqa: BLE001
            # Non-connection errors (auth, 400, model errors, etc.) —
            # don't retry; they won't improve from waiting.
            api_error = exc
            break

    if response is None:
        logger.warning(
            "cognify_extract: LLM API call failed after %d attempts: %s",
            _API_MAX_RETRIES, api_error,
        )
        return {
            "lessons": [],
            "decisions": [],
            "_usage": _empty_usage,
            "_failure": "api",
            "_failure_detail": str(api_error)[:500],
        }

    usage = response.usage
    token_usage = {
        "input_tokens": getattr(usage, "input_tokens", 0),
        "output_tokens": getattr(usage, "output_tokens", 0),
        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
        "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
    }
    from cognify._anthropic_auth import response_text

    text = response_text(response).strip()
    # The OAuth path doesn't allow assistant prefill (see comment above
    # the messages.create call) so the response is whatever Sonnet
    # emitted. _parse_json_with_fallbacks handles:
    #   - direct {...} JSON (the happy path with the strengthened user
    #     prompt steering Sonnet toward JSON-only output)
    #   - markdown ```json fences anywhere in the response
    #   - JSON embedded in prose (substring search)
    result = _parse_json_with_fallbacks(text)
    if result is None:
        # Log a sample of the offending payload so we can spot patterns
        # (e.g. model returning a refusal, hitting max_tokens mid-string,
        # wrapping in extra preamble). Keep it short to avoid flooding logs.
        payload_sample = text[:400] if text else "<empty>"
        logger.warning(
            "cognify_extract: JSON parse failed across all strategies; "
            "model output sample: %r",
            payload_sample,
        )
        return {
            "lessons": [],
            "decisions": [],
            "_usage": token_usage,  # the API call DID succeed; preserve spend
            "_failure": "json_parse",
            "_failure_detail": f"all parse strategies exhausted; sample={payload_sample!r}",
        }
    result["_usage"] = token_usage
    if not result.get("lessons") and not result.get("decisions"):
        result["_failure"] = "empty"
    return result


def _resolve_auth() -> dict[str, Any] | None:
    """Resolve Anthropic credential to SDK kwargs (api_key= or auth_token=).

    Accepts a Console API key OR a subscription OAuth token; the SDK
    routes them through different headers. See cognify._anthropic_auth
    for the resolution order and accepted env var names.
    """
    from cognify._anthropic_auth import resolve_anthropic_auth
    return resolve_anthropic_auth()


def _get_api_key() -> str | None:
    """Deprecated — kept for any external callers. Use _resolve_auth()."""
    import os
    return os.environ.get("ANTHROPIC_API_KEY")


def _parse_json_with_fallbacks(text: str) -> dict | None:
    """Best-effort parse of an LLM JSON response.

    Tries a sequence of strategies — the first that yields a dict with
    'lessons' and/or 'decisions' wins. Returns None if all fail.

    1. Direct `json.loads(text)` — the normal happy path. With assistant
       prefill (`{` as the prefill) this works for ~all real responses.
    2. Strip markdown code fences (```json ... ``` or ``` ... ```)
       anywhere in the text. The 2026-05-17 brightbot retry observed
       Sonnet occasionally pre-amble-ing with "Here's the breakdown:"
       followed by a fenced block.
    3. Find the first balanced `{...}` substring in the text and try
       parsing that. Handles model output that puts JSON in the middle
       of explanatory prose.
    """
    # Strategy 1: direct parse.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strategy 2: strip markdown code fences anywhere in the response.
    # Patterns we've observed:
    #   ```json\n{...}\n```
    #   ```\n{...}\n```
    import re  # noqa: PLC0415

    fence_match = re.search(
        r"```(?:json)?\s*\n(.*?)\n```",
        text,
        re.DOTALL,
    )
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    # Strategy 3: find the largest balanced {…} substring and try parse.
    # Doesn't try to be clever about nested strings; relies on json.loads
    # to fail-fast if the candidate isn't actually JSON.
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        candidate = text[first_brace : last_brace + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    return None


def _upsert_memory(
    conn: Any,
    *,
    project_id: Any,
    kind: str,
    title: str,
    content: str,
    session_id: str,
    reextract: bool = False,
    reextract_meta: str | None = None,
    quote: str | None = None,
    report: dict | None = None,
) -> tuple[UUID, bool]:
    """Insert an extracted lesson/decision memory row. Returns (id, was_new).

    Identity, cheapest check first (each one skips the embedding call and
    the relation check):

    1. Same session, kind and title as a live row → that row (migration
       037's idx_memory_atom_title_unique is the DB backstop).
    2. Same content key (``content_key``: uuid5 over project, kind and the
       normalized title) as a live row → that row. A hit from ANOTHER
       session is a re-observation: the row is reinforced (the relation
       check's "same" nudge) and the session is appended to
       applies_when.also_seen_in. A hit from the same session is a plain
       re-run and changes nothing.
    3. The write-time relation check (embedding near-duplicates; off by
       default) may still answer "same".

    A new row's id IS its content key, unless an archived row already holds
    that id (a re-extraction after ``_archive_prior_extracts``): archived
    rows stay as they are and the new row gets a random id. The key is also
    stored in applies_when.content_key, so lookups find rows of both kinds.

    ``report["outcome"]`` (when a dict is passed) is set to one of
    created / duplicate / merged / relation_same.

    Memory schema constraints:
    - kind must be in: chunk, decision, pattern, issue, session_summary
      Extracted lessons use kind='pattern'; decisions use kind='decision'.
    - tier: 'lesson' for extracted rows.
    - No metadata column exists; extraction metadata (source_session,
      reextracted_from, quote, content_key) lives in applies_when.
    """
    report = report if report is not None else {}
    # Map semantic kind to valid DB kind values.
    # Lessons → kind='pattern'; decisions → kind='decision'.
    # Migration 037 made provenance_id load-bearing for atoms too —
    # session_id IS the raw_sessions.id of the source session, and
    # the new idx_memory_atom_title_unique allows N atoms per
    # (session, kind) as long as titles differ.
    db_kind = "pattern" if kind == "lesson" else "decision"
    key = str(content_key(project_id, db_kind, title, content))

    # applies_when is a JSONB column for extract-related metadata.
    # source_session is kept here for backwards compat with any
    # consumer that reads it directly, but provenance_id is now the
    # canonical link to the source session.
    applies_when: dict = {"source_session": session_id, "content_key": key}
    if quote:
        # The verified evidence for this atom (see verify_quote).
        applies_when["quote"] = quote
    if reextract and reextract_meta:
        applies_when["reextracted_from"] = reextract_meta

    # (1) Idempotency check: same provenance_id + kind + title means we've
    # already extracted this same atom from this session. Skip the
    # round-trip and short-circuit; the DB constraint is the backstop.
    existing = _find_title_duplicate(conn, project_id, db_kind, title, session_id)
    if existing:
        report["outcome"] = "duplicate"
        return existing, False

    # (2) Content-derived identity. Indexed: the primary key, and the GIN
    # index on applies_when for rows whose id could not be the key.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, provenance_id FROM devbrain.memory "
            "WHERE project_id = %s AND kind = %s AND archived_at IS NULL "
            "  AND (id = %s::uuid OR applies_when @> %s::jsonb) "
            "ORDER BY created_at LIMIT 1",
            (project_id, db_kind, key, json.dumps({"content_key": key})),
        )
        hit = cur.fetchone()
    if hit:
        hit_id, hit_session = hit
        if str(hit_session) == str(session_id):
            report["outcome"] = "duplicate"
        else:
            _merge_observation(conn, hit_id, session_id)
            report["outcome"] = "merged"
        return hit_id, False

    # Insert new extract row with provenance_id = the source session.
    cols = [
        "project_id", "kind", "title", "content",
        "tier", "strength", "applies_when", "extraction_version",
        "provenance_id",
    ]
    vals: list = [
        project_id, db_kind, title, content, "lesson", 1.0,
        json.dumps(applies_when), CURRENT_EXTRACTION_VERSION,
        session_id,
    ]
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM devbrain.memory WHERE id = %s::uuid", (key,))
        key_taken = cur.fetchone() is not None
    if not key_taken:
        cols.insert(0, "id")
        vals.insert(0, key)

    # Embed the atom so deep_search (which filters embedding IS NOT NULL)
    # can find it. Skipping this is what left ~22k decision/pattern atoms
    # invisible to search. embed_text degrades to None if Ollama is down —
    # the row is still written and a reembed pass backfills the vector.
    placeholders_list = ["%s"] * len(cols)
    emb = embed_text(f"{title}\n{content}" if title else content)
    emb_literal = to_vector_literal(emb) if emb is not None else None
    if emb_literal is not None:
        cols.append("embedding")
        vals.append(emb_literal)
        placeholders_list.append("%s::vector")

    # (3) Write-time relation check (write_path.relation_check; off by
    # default). A "same" verdict means this fact is already known in the
    # project: the existing atom is reinforced and nothing is written. Any
    # failure in the check falls through to the plain write below.
    rel_cfg = relate.get_config()
    pre = relate.precheck(
        conn, rel_cfg,
        project_id=project_id, kind=db_kind, title=title, content=content,
        embedding_literal=emb_literal, source="extract", source_ref=str(session_id),
    )
    if pre.skip_insert:
        report["outcome"] = "relation_same"
        return pre.existing_id, False

    placeholders = ", ".join(placeholders_list)
    with conn.cursor() as cur:
        # DO NOTHING (no target) covers the id and 037's title index: a
        # concurrent writer of the same atom loses quietly.
        cur.execute(
            f"INSERT INTO devbrain.memory ({', '.join(cols)}) "
            f"VALUES ({placeholders}) "
            "ON CONFLICT DO NOTHING "
            "RETURNING id",
            vals,
        )
        row = cur.fetchone()
    if row is None:
        conn.commit()
        winner = _find_title_duplicate(conn, project_id, db_kind, title, session_id)
        report["outcome"] = "duplicate"
        return (winner or key), False
    mid = row[0]
    pre.finish(
        conn, project_id=project_id, new_id=mid, cfg=rel_cfg,
        source="extract", source_ref=str(session_id),
    )
    conn.commit()
    report["outcome"] = "created"
    return mid, True


# Sessions remembered per merged atom (applies_when.also_seen_in).
_MAX_ALSO_SEEN = 20


def _find_title_duplicate(
    conn: Any, project_id: Any, db_kind: str, title: str, session_id: str
) -> Any:
    """Live atom from the same session with the same kind and title."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.memory "
            "WHERE project_id = %s "
            "  AND kind = %s "
            "  AND tier = 'lesson' "
            "  AND title = %s "
            "  AND archived_at IS NULL "
            "  AND provenance_id = %s::uuid",
            (project_id, db_kind, title, session_id),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _merge_observation(conn: Any, memory_id: Any, session_id: str) -> None:
    """Another session produced an atom with the same content key: reinforce
    the existing row (as a relation-check "same" verdict does) and note the
    session in applies_when.also_seen_in (capped at _MAX_ALSO_SEEN)."""
    relate.reinforce(conn, memory_id)
    seen = "coalesce(applies_when -> 'also_seen_in', '[]'::jsonb)"
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET applies_when = jsonb_set("
            f"  coalesce(applies_when, '{{}}'::jsonb), '{{also_seen_in}}', "
            f"  {seen} || to_jsonb(%s::text)) "
            f"WHERE id = %s AND NOT {seen} ? %s "
            f"  AND jsonb_array_length({seen}) < %s",
            (str(session_id), memory_id, str(session_id), _MAX_ALSO_SEEN),
        )
    conn.commit()


def _archive_prior_extracts(
    conn: Any, session_id: str, project_id: Any
) -> int:
    """Archive existing extracted rows for a session before re-extraction.

    Sets archived_at on rows with applies_when->>'source_session' = session_id
    and tier = 'lesson'. These are the rows produced by prior extract runs.
    Never deletes — HIPAA audit trail.
    Returns the count of rows archived.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory "
            "SET archived_at = now() "
            "WHERE project_id = %s "
            "  AND tier = 'lesson' "
            "  AND archived_at IS NULL "
            "  AND (applies_when->>'source_session') = %s",
            (project_id, session_id),
        )
        count = cur.rowcount
    conn.commit()
    logger.info(
        "cognify_extract: archived %d prior extracts for session %s",
        count,
        session_id,
    )
    return count
