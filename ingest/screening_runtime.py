"""Ingest-side glue for injection screening: config switches and the
optional local-model second pass. Rules live in screening.py."""

from __future__ import annotations

from urllib.parse import urlparse

import screening

from config import (
    SCREENING_ALLOW_REMOTE_MODEL,
    SCREENING_ENABLED,
    SCREENING_MODEL,
    SCREENING_MODEL_CHECK,
    SCREENING_URL,
)

_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def _model_allowed(url: str) -> bool:
    if SCREENING_ALLOW_REMOTE_MODEL:
        return True
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOOPBACK or host.startswith("127.")


def chunk_flags(text: str, *, chat=None) -> list[str] | None:
    """Flags to store for a chunk, or None. With model_check on, the local
    model must agree before a rule hit is stored; a failed model call keeps
    the rule verdict (fail-closed)."""
    if not SCREENING_ENABLED:
        return None
    flags = screening.flags_for(text)
    if flags and SCREENING_MODEL_CHECK and _model_allowed(SCREENING_URL):
        verdict = screening.model_confirms(
            text, url=SCREENING_URL, model=SCREENING_MODEL, chat=chat
        )
        if verdict is False:
            return None
    return flags


def redact_for_model(text: str) -> str:
    """Transcript text with flagged segments withheld (summaries)."""
    if not SCREENING_ENABLED:
        return text
    return screening.redact(text)[0]
