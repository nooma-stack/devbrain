"""Mechanical session-closure detection from Claude Code transcripts.

The ground truth for "did this session close out properly?" is the
transcript itself: a session that called ``end_session`` carries a
``tool_use`` block named ``mcp__<server>__end_session`` in its jsonl.
No sentinel convention is needed and no id-mapping heuristics — the same
scan also extracts the devbrain ``conversation_uuid`` linkage (from
``start_session`` results / ``breadcrumb`` inputs / ``end_session``'s
``session_id``) and the position of the LAST breadcrumb, which is what
lets the backfill worker summarize only the un-checkpointed tail.

This module is pure parsing — no DB, no network — so it is trivially
testable and reusable by any monitor/scanner.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# devbrain and brightbrain expose the same tool names; foreign servers
# with an end_session-shaped tool would also count, which is fine — the
# scan answers "did ANY session-closure ritual happen", not "which brain".
_TOOL_RE = re.compile(r"^mcp__[A-Za-z0-9_-]+__(end_session|breadcrumb|start_session)$")


@dataclass
class TranscriptFacts:
    path: str
    total_lines: int = 0
    closed: bool = False
    end_session_line: int | None = None
    conversation_uuid: str | None = None
    last_breadcrumb_line: int | None = None
    breadcrumbs: list[dict] = field(default_factory=list)  # tool inputs, in order
    parse_errors: int = 0


def _iter_tool_uses(entry: dict):
    msg = entry.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, list):
        return
    for item in content:
        if isinstance(item, dict) and item.get("type") == "tool_use":
            yield item


def parse_transcript(path: str | Path) -> TranscriptFacts:
    """Single pass over a session jsonl; returns closure + linkage facts."""
    facts = TranscriptFacts(path=str(path))
    with open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            facts.total_lines = lineno
            if "mcp__" not in line:
                continue  # cheap pre-filter; tool_use lines always contain it
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                facts.parse_errors += 1
                continue
            for tool in _iter_tool_uses(entry):
                m = _TOOL_RE.match(str(tool.get("name", "")))
                if not m:
                    continue
                kind = m.group(1)
                tool_input = tool.get("input") or {}
                if kind == "end_session":
                    facts.closed = True
                    facts.end_session_line = lineno
                    facts.conversation_uuid = (
                        tool_input.get("session_id") or facts.conversation_uuid
                    )
                elif kind == "breadcrumb":
                    facts.last_breadcrumb_line = lineno
                    facts.breadcrumbs.append(tool_input)
                    facts.conversation_uuid = (
                        tool_input.get("conversation_uuid") or facts.conversation_uuid
                    )
                # start_session's uuid comes back in the RESULT, not the
                # input — breadcrumb/end_session inputs are the reliable
                # places to read it mechanically.
    return facts


def extract_text(path: str | Path, from_line: int = 0, max_chars: int | None = None,
                 head_tail_chars: int | None = None) -> str:
    """Readable transcript text (user/assistant blocks) from ``from_line`` on.

    ``max_chars`` truncates from the FRONT (keeps the beginning);
    ``head_tail_chars`` instead keeps the first and last halves of the
    budget — for summarization the ENDING is where outcomes live, so
    head+tail is the right cap shape when the tail matters.
    """
    parts: list[str] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            if lineno < from_line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = entry.get("message") or {}
            role = msg.get("role", "")
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                parts.append(f"[{role}] {content}")
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        text = item.get("text", "")
                        if text.strip():
                            parts.append(f"[{role}] {text}")
    text = "\n".join(parts)
    if head_tail_chars is not None and len(text) > head_tail_chars:
        half = head_tail_chars // 2
        return (text[:half] + "\n\n[... middle elided for length ...]\n\n"
                + text[-half:])
    if max_chars is not None:
        return text[:max_chars]
    return text


# ── USF fallback (transcript file gone; DB raw_content survives) ────────────
#
# raw_sessions.raw_content stores the Universal Session Format JSON, which
# preserves tool calls with full name + args. That gives the DB copy FULL
# parity with the on-disk jsonl for closure purposes: end_session/breadcrumb
# detection, conversation_uuid linkage, and last-breadcrumb position — the
# only difference is that positions are MESSAGE INDICES, not line numbers.

def parse_usf(raw_json: str, path: str = "<db:raw_content>") -> TranscriptFacts:
    facts = TranscriptFacts(path=path)
    try:
        usf = json.loads(raw_json)
    except json.JSONDecodeError:
        facts.parse_errors += 1
        return facts
    messages = usf.get("messages") or []
    facts.total_lines = len(messages)
    for idx, msg in enumerate(messages, 1):
        for call in (msg.get("tool_calls") or []):
            m = _TOOL_RE.match(str(call.get("tool", "")))
            if not m:
                continue
            kind = m.group(1)
            args = call.get("args") or {}
            if kind == "end_session":
                facts.closed = True
                facts.end_session_line = idx
                facts.conversation_uuid = (
                    args.get("session_id") or facts.conversation_uuid)
            elif kind == "breadcrumb":
                facts.last_breadcrumb_line = idx
                facts.breadcrumbs.append(args)
                facts.conversation_uuid = (
                    args.get("conversation_uuid") or facts.conversation_uuid)
    return facts


def extract_usf_text(raw_json: str, from_index: int = 0,
                     head_tail_chars: int | None = None) -> str:
    try:
        usf = json.loads(raw_json)
    except json.JSONDecodeError:
        return ""
    parts = []
    for idx, msg in enumerate(usf.get("messages") or [], 1):
        if idx < from_index:
            continue
        content = msg.get("content") or ""
        if str(content).strip():
            parts.append(f"[{msg.get('role', '')}] {content}")
    text = "\n".join(parts)
    if head_tail_chars is not None and len(text) > head_tail_chars:
        half = head_tail_chars // 2
        return (text[:half] + "\n\n[... middle elided for length ...]\n\n"
                + text[-half:])
    return text
