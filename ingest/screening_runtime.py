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


def _confirmed(text: str, flags: list[str] | None, chat) -> list[str] | None:
    """With model_check on, the local model must agree before a rule hit is
    stored; a failed model call keeps the rule verdict (fail-closed)."""
    if flags and SCREENING_MODEL_CHECK and _model_allowed(SCREENING_URL):
        verdict = screening.model_confirms(
            text, url=SCREENING_URL, model=SCREENING_MODEL, chat=chat
        )
        if verdict is False:
            return None
    return flags


def chunk_flags(text: str, *, chat=None) -> list[str] | None:
    """Flags for a piece of flat text (no role information), or None."""
    if not SCREENING_ENABLED:
        return None
    return _confirmed(text, screening.flags_for(text), chat)


def session_chunk_flags(messages, chunks, *, chat=None) -> list[list[str] | None]:
    """Flags for each chunk of a session, from the structured messages (so
    the person's own turns are known and a payload split across chunks
    still flags every chunk it touches)."""
    if not SCREENING_ENABLED:
        return [None] * len(chunks)
    out = []
    for chunk, flags in zip(
        chunks, screening.chunk_flags_from_messages(messages, chunks)
    ):
        out.append(_confirmed(chunk.content, flags, chat))
    return out


def session_text_for_model(session) -> str:
    """UniversalSession.to_text() with flagged messages withheld: the
    summarizer's input."""
    if not SCREENING_ENABLED:
        return session.to_text()
    contents, _ = screening.redact_messages(session.messages)
    lines = []
    for msg, content in zip(session.messages, contents):
        prefix = f"[{msg.role}]"
        if msg.timestamp:
            prefix = f"[{msg.timestamp}] [{msg.role}]"
        lines.append(f"{prefix} {content[:2000]}")
    return "\n".join(lines).replace("\x00", "")
