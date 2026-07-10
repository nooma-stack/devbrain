"""Codex / ChatGPT-desktop adapter tests (no DB, no network).

Run from the ingest/ directory: ``cd ingest && python -m pytest tests/``
(the adapters import ``config`` from the ingest root).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adapters.codex import CodexAdapter
from pipeline import ADAPTERS, detect_adapter


def _write_jsonl(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Pad with a trailing comment-ish entry so files clear any size floor.
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return path


def _session_meta(**overrides) -> dict:
    payload = {
        "id": "sess-123",
        "cwd": "/Users/dev/lighthouse/brightbot",
        "originator": "Codex Desktop",
        "cli_version": "0.142.5",
        "model_provider": "openai",
    }
    payload.update(overrides)
    return {"timestamp": "2026-07-10T12:00:00Z", "type": "session_meta", "payload": payload}


def _response_item(role: str, text: str) -> dict:
    return {
        "timestamp": "2026-07-10T12:01:00Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [
                {"type": "input_text" if role == "user" else "output_text", "text": text}
            ],
        },
    }


def _event_msg(msg_type: str, message: str) -> dict:
    return {
        "timestamp": "2026-07-10T12:01:00Z",
        "type": "event_msg",
        "payload": {"type": msg_type, "message": message},
    }


# ─── detect() ────────────────────────────────────────────────────────


def test_detects_codex_home_files():
    a = CodexAdapter()
    assert a.detect(Path("/Users/x/.codex/sessions/2026/07/10/rollout-a.jsonl"))
    assert a.detect(Path("/Users/x/.codex/archived_sessions/rollout-b.jsonl.zst"))


def test_detects_drop_zone_codex_subdir_only():
    a = CodexAdapter()
    assert a.detect(Path("/Users/lhtdev/ingest-incoming/mark/codex/rollout-a.jsonl"))
    assert not a.detect(
        Path("/Users/lhtdev/ingest-incoming/mark/projects/-Users-mark-lighthouse-brightbot/x.jsonl")
    )
    assert not a.detect(Path("/Users/x/somewhere/random.jsonl"))
    # A dev whose drop-zone username is "codex" must NOT be claimed — their
    # files are Claude Code transcripts (positional segment check).
    assert not a.detect(
        Path("/Users/lhtdev/ingest-incoming/codex/projects/-Users-c-lighthouse-x/y.jsonl")
    )
    # Deeper nesting under the codex source dir still matches.
    assert a.detect(
        Path("/Users/lhtdev/ingest-incoming/mark/codex/2026/07/rollout-b.jsonl.zst")
    )


def test_pipeline_order_codex_wins_drop_zone():
    """Codex must precede claude_code or drop-zone codex files are stolen."""
    names = [type(a).__name__ for a in ADAPTERS]
    assert names.index("CodexAdapter") < names.index("ClaudeCodeAdapter")

    picked = detect_adapter(
        Path("/Users/lhtdev/ingest-incoming/mark/codex/rollout-a.jsonl")
    )
    assert type(picked).__name__ == "CodexAdapter"
    # Claude Code drop-zone files still go to claude_code.
    picked = detect_adapter(
        Path("/Users/lhtdev/ingest-incoming/mark/projects/-p/x.jsonl")
    )
    assert type(picked).__name__ == "ClaudeCodeAdapter"


# ─── parse() ─────────────────────────────────────────────────────────


def test_parses_response_item_messages(tmp_path):
    fp = _write_jsonl(
        tmp_path / ".codex" / "sessions" / "rollout-x.jsonl",
        [
            _session_meta(),
            _response_item("user", "How do I run the tests?"),
            _response_item("assistant", "Use pytest from the repo root."),
        ],
    )
    s = CodexAdapter().parse(fp)
    assert s is not None
    assert s.session_id == "sess-123"
    assert [m.role for m in s.messages] == ["user", "assistant"]
    assert s.messages[0].content == "How do I run the tests?"


def test_event_msg_is_fallback_not_duplicate(tmp_path):
    """Desktop rollouts carry messages in BOTH streams — no duplication."""
    fp = _write_jsonl(
        tmp_path / ".codex" / "sessions" / "rollout-dup.jsonl",
        [
            _session_meta(),
            _response_item("user", "question text"),
            _event_msg("user_message", "question text"),
            _response_item("assistant", "answer text"),
            _event_msg("agent_message", "answer text"),
        ],
    )
    s = CodexAdapter().parse(fp)
    assert s is not None
    assert len(s.messages) == 2  # response_item authoritative


def test_event_msg_fallback_when_no_response_items(tmp_path):
    fp = _write_jsonl(
        tmp_path / ".codex" / "sessions" / "rollout-em.jsonl",
        [
            _session_meta(session_id="sess-456", id=None),
            _event_msg("user_message", "only in event stream"),
            _event_msg("agent_message", "reply in event stream"),
            _event_msg("token_count", ""),  # ignored type
        ],
    )
    s = CodexAdapter().parse(fp)
    assert s is not None
    assert s.session_id == "sess-456"  # payload.session_id fallback
    assert [m.role for m in s.messages] == ["user", "assistant"]
    assert s.messages[1].content == "reply in event stream"


def test_unknown_line_types_tolerated(tmp_path):
    fp = _write_jsonl(
        tmp_path / ".codex" / "sessions" / "rollout-unk.jsonl",
        [
            _session_meta(),
            {"timestamp": "t", "type": "world_state", "payload": {"x": 1}},
            {"timestamp": "t", "type": "inter_agent_communication_metadata", "payload": {}},
            _response_item("user", "hello"),
            "not-a-dict-line",
        ],
    )
    s = CodexAdapter().parse(fp)
    assert s is not None and len(s.messages) == 1


def test_empty_file_returns_none(tmp_path):
    fp = _write_jsonl(tmp_path / ".codex" / "sessions" / "rollout-empty.jsonl", [_session_meta()])
    assert CodexAdapter().parse(fp) is None


def test_zst_rollout_parses_when_zstandard_available(tmp_path):
    zstandard = pytest.importorskip("zstandard")
    raw = b"".join(
        (json.dumps(e) + "\n").encode()
        for e in [_session_meta(), _response_item("user", "compressed q")]
    )
    fp = tmp_path / ".codex" / "archived_sessions" / "rollout-z.jsonl.zst"
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_bytes(zstandard.ZstdCompressor().compress(raw))
    s = CodexAdapter().parse(fp)
    assert s is not None
    assert s.messages[0].content == "compressed q"
