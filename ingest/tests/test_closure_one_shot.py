"""One-shot sessions (one prompt, no tools) are skipped by the closure
scanner; real sessions and unparsed rows are not (Nooma junk, 2026-10-19)."""

import json

from session_closure import parse_transcript, parse_usf


def _usf(messages):
    return json.dumps({"usf_version": "1.0", "messages": messages})


def test_usf_one_shot():
    facts = parse_usf(_usf([
        {"role": "user", "content": "## Brief ..."},
        {"role": "assistant", "content": '{"findings": []}'},
    ]))
    assert facts.one_shot


def test_usf_with_tools_or_turns_is_not_one_shot():
    tools = parse_usf(_usf([
        {"role": "user", "content": "fix it"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"tool": "Bash", "args": {"command": "ls"}}]},
    ]))
    turns = parse_usf(_usf([
        {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"}, {"role": "assistant", "content": "d"},
    ]))
    assert not tools.one_shot
    assert not turns.one_shot


def test_pre_usf_plain_text_is_not_one_shot():
    assert not parse_usf("[user] plain text transcript").one_shot


def test_jsonl_counts(tmp_path):
    path = tmp_path / "s.jsonl"
    lines = [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "ok"}]}},
    ]
    path.write_text("\n".join(json.dumps(x, separators=(",", ":")) for x in lines))
    facts = parse_transcript(path)
    assert (facts.user_turns, facts.tool_calls) == (1, 1)
    assert not facts.one_shot
    path.write_text(json.dumps(lines[0], separators=(",", ":")))
    assert parse_transcript(path).one_shot
