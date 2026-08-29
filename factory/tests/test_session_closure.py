"""Tests for the mechanical session-closure scanner (ingest/session_closure.py).

The detector's whole premise: a properly-closed session carries an
``mcp__*__end_session`` tool_use in its own transcript, and the same scan
yields the conversation_uuid linkage and the last-breadcrumb position for
tail-only backfill. These tests pin that contract on synthetic jsonl.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "ingest"))

from session_closure import extract_text, parse_transcript  # noqa: E402


def _line(role: str, content):
    return json.dumps({"message": {"role": role, "content": content}})


def _tool(name: str, tool_input: dict):
    return _line("assistant", [{"type": "tool_use", "name": name,
                                "input": tool_input}])


def _write(tmp_path, lines):
    p = tmp_path / "session.jsonl"
    p.write_text("\n".join(lines) + "\n")
    return p


def test_closed_session_detected_with_uuid(tmp_path):
    p = _write(tmp_path, [
        _line("user", "do the thing"),
        _tool("mcp__brightbrain__breadcrumb",
              {"conversation_uuid": "abc-123", "title": "CHECKPOINT one",
               "content": "did half"}),
        _line("assistant", "ok done"),
        _tool("mcp__brightbrain__end_session",
              {"project": "brightbot", "session_id": "abc-123",
               "summary": "all done"}),
    ])
    facts = parse_transcript(p)
    assert facts.closed
    assert facts.conversation_uuid == "abc-123"
    assert facts.last_breadcrumb_line == 2
    assert facts.end_session_line == 4
    assert len(facts.breadcrumbs) == 1


def test_unclosed_session_detected(tmp_path):
    p = _write(tmp_path, [
        _line("user", "start"),
        _tool("mcp__devbrain__breadcrumb",
              {"conversation_uuid": "u-9", "title": "CHECKPOINT",
               "content": "mid"}),
        _line("assistant", "working..."),
    ])
    facts = parse_transcript(p)
    assert not facts.closed
    assert facts.conversation_uuid == "u-9"
    assert facts.last_breadcrumb_line == 2


def test_mentions_in_prose_do_not_count_as_closure(tmp_path):
    # The word "end_session" in TEXT (instructions, discussion) must not
    # register — only a tool_use block does.
    p = _write(tmp_path, [
        _line("user", "remember to call end_session when done"),
        _line("assistant", "I will call mcp__devbrain__end_session later."),
    ])
    facts = parse_transcript(p)
    assert not facts.closed
    assert facts.conversation_uuid is None


def test_extract_text_head_tail_keeps_the_ending(tmp_path):
    lines = [_line("assistant", f"chunk {i} " + "x" * 50) for i in range(200)]
    p = _write(tmp_path, lines)
    text = extract_text(p, head_tail_chars=1000)
    assert len(text) < 1200
    assert "chunk 0" in text
    assert "chunk 199" in text          # the ending survives the cap
    assert "middle elided" in text


def test_extract_text_from_line_skips_checkpointed_prefix(tmp_path):
    p = _write(tmp_path, [
        _line("assistant", "EARLY-MARKER work"),
        _tool("mcp__devbrain__breadcrumb", {"conversation_uuid": "u",
                                            "title": "cp", "content": "c"}),
        _line("assistant", "LATE-MARKER work"),
    ])
    facts = parse_transcript(p)
    tail = extract_text(p, from_line=facts.last_breadcrumb_line or 0)
    assert "LATE-MARKER" in tail
    assert "EARLY-MARKER" not in tail


# ── USF (DB raw_content) parity tests ───────────────────────────────────────

def _usf(messages):
    import json as _json
    return _json.dumps({"usf_version": "1.0", "messages": messages})


def test_usf_closed_session_with_chain():
    from session_closure import parse_usf
    raw = _usf([
        {"role": "user", "content": "go", "tool_calls": []},
        {"role": "assistant", "content": "",
         "tool_calls": [{"tool": "mcp__devbrain__breadcrumb",
                         "args": {"conversation_uuid": "cu-1", "title": "cp",
                                  "content": "mid"}}]},
        {"role": "assistant", "content": "",
         "tool_calls": [{"tool": "mcp__devbrain__end_session",
                         "args": {"session_id": "cu-1", "summary": "done"}}]},
    ])
    facts = parse_usf(raw)
    assert facts.closed
    assert facts.conversation_uuid == "cu-1"
    assert facts.last_breadcrumb_line == 2      # message INDEX, not line


def test_usf_unclosed_and_text_extraction():
    from session_closure import extract_usf_text, parse_usf
    raw = _usf([
        {"role": "assistant", "content": "EARLY work", "tool_calls": []},
        {"role": "assistant", "content": "",
         "tool_calls": [{"tool": "mcp__devbrain__breadcrumb",
                         "args": {"conversation_uuid": "u2", "title": "cp",
                                  "content": "c"}}]},
        {"role": "assistant", "content": "LATE work", "tool_calls": []},
    ])
    facts = parse_usf(raw)
    assert not facts.closed
    tail = extract_usf_text(raw, from_index=facts.last_breadcrumb_line or 0)
    assert "LATE" in tail and "EARLY" not in tail
