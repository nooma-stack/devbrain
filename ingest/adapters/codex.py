"""Codex / ChatGPT-desktop transcript adapter.

Parses ``~/.codex/sessions/**/rollout-*.jsonl`` session files into the
Universal Session Format. As of 2026-07-09 OpenAI's unified ChatGPT
desktop app (bundle ``com.openai.codex`` — Chat + Work + Codex tabs) IS
the Codex app, and desktop Work/Codex threads land in the same rollout
files (``originator: "Codex Desktop"``), so this one adapter covers both
the CLI and the desktop app.

Rollout JSONL line types (undocumented format — parse defensively,
tolerate unknown types):

- type=session_meta: {id|session_id, timestamp, cwd, originator,
  cli_version, model_provider, ...}
- type=response_item: {type: "message", role: user|assistant|developer,
  content: [{type, text}]} plus function_call / function_call_output
- type=event_msg: {type: user_message|agent_message|..., message: str}
  — the chat-narrative stream; it OVERLAPS response_item messages, so it
  is used only as a FALLBACK when a file yields no response_item
  messages (guards against format drift between app versions).

Archived sessions may be Zstandard-compressed (``*.jsonl.zst``);
handled when the optional ``zstandard`` package is available.

Drop-zone support: remote devs ship codex rollouts to the Studio under
``~/ingest-incoming/<dev>/codex/…`` — detect() claims that subtree, and
CodexAdapter is registered BEFORE ClaudeCodeAdapter in the pipeline so
the latter's greedy ".jsonl under ingest-incoming" match can't steal
these files.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path

from .base import UniversalMessage, UniversalSession
from .claude_code import _lookup_slug


def _is_rollout_file(file_path: Path) -> bool:
    name = file_path.name
    return name.endswith(".jsonl") or name.endswith(".jsonl.zst")


def _open_text(file_path: Path):
    """Open a rollout file as text, transparently decompressing .zst."""
    if file_path.name.endswith(".jsonl.zst"):
        try:
            import zstandard
        except ImportError:
            print(f"  Skipping {file_path}: zstandard not installed")
            return None
        fh = open(file_path, "rb")
        stream = zstandard.ZstdDecompressor().stream_reader(fh)
        return io.TextIOWrapper(stream, encoding="utf-8")
    return open(file_path, encoding="utf-8")


def _iter_entries(file_path: Path) -> Iterator[dict]:
    f = _open_text(file_path)
    if f is None:
        return
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                yield entry


class CodexAdapter:
    app_name = "codex"
    file_patterns = ["*.jsonl", "*.jsonl.zst"]

    def detect(self, file_path: Path) -> bool:
        if not _is_rollout_file(file_path):
            return False
        s = str(file_path)
        if ".codex" in s:
            return True
        # Remote-dev drop zone: ingest-incoming/<dev>/codex/… (must be
        # claimed here because ClaudeCodeAdapter matches ANY .jsonl under
        # ingest-incoming; pipeline ADAPTERS order puts codex first).
        return "ingest-incoming" in s and "/codex/" in s

    def detect_project(self, file_path: Path) -> str | None:
        """Infer project from session_meta cwd field.

        Looks up cwd in ingest.project_mappings / auto_project_roots.
        """
        try:
            for entry in _iter_entries(file_path):
                if entry.get("type") == "session_meta":
                    cwd = (entry.get("payload") or {}).get("cwd", "")
                    return _lookup_slug(cwd)
        except Exception:
            pass
        return None

    def parse(self, file_path: Path) -> UniversalSession | None:
        """Parse a Codex / ChatGPT-desktop rollout file."""
        messages: list[UniversalMessage] = []
        event_messages: list[UniversalMessage] = []
        session_id: str | None = None
        model: str | None = None
        first_ts: str | None = None
        last_ts: str | None = None
        files_changed: set[str] = set()

        try:
            for entry in _iter_entries(file_path):
                ts = entry.get("timestamp")
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts

                entry_type = entry.get("type", "")
                payload = entry.get("payload", {})
                if not isinstance(payload, dict):
                    continue

                if entry_type == "session_meta":
                    session_id = payload.get("id") or payload.get("session_id")
                    model = payload.get("model_provider")

                elif entry_type == "response_item":
                    role = payload.get("role", "")
                    if role == "developer":
                        continue  # Skip system/developer messages

                    content_parts: list[str] = []
                    tool_calls: list[dict] = []
                    raw_content = payload.get("content", [])

                    if isinstance(raw_content, list):
                        for block in raw_content:
                            if not isinstance(block, dict):
                                continue
                            block_type = block.get("type", "")
                            if block_type in ("input_text", "output_text", "text"):
                                text = block.get("text", "")
                                if text:
                                    content_parts.append(text)
                            elif block_type == "function_call":
                                tool_calls.append({
                                    "tool": block.get("name", ""),
                                    "args": block.get("arguments", ""),
                                })
                            elif block_type == "function_call_output":
                                output = block.get("output", "")
                                if output:
                                    content_parts.append(
                                        f"[tool_result] {str(output)[:500]}"
                                    )
                    elif isinstance(raw_content, str):
                        content_parts.append(raw_content)

                    # Map codex roles to standard roles
                    std_role = "user" if role == "user" else "assistant"

                    content = "\n".join(content_parts).strip()
                    if content or tool_calls:
                        messages.append(UniversalMessage(
                            role=std_role,
                            timestamp=ts,
                            content=content,
                            tool_calls=tool_calls,
                        ))

                        # Track file paths from tool calls
                        for tc in tool_calls:
                            args = tc.get("args", "")
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except (json.JSONDecodeError, TypeError):
                                    pass
                            if isinstance(args, dict):
                                for key in ("file_path", "path", "file"):
                                    val = args.get(key)
                                    if isinstance(val, str) and "/" in val:
                                        files_changed.add(val)

                elif entry_type == "event_msg":
                    # Chat-narrative stream (desktop app). Collected
                    # separately — response_item stays authoritative; this
                    # is the fallback if a future format drops it.
                    msg_type = payload.get("type", "")
                    text = payload.get("message", "")
                    if msg_type in ("user_message", "agent_message") and text:
                        event_messages.append(UniversalMessage(
                            role="user"
                            if msg_type == "user_message"
                            else "assistant",
                            timestamp=ts,
                            content=str(text),
                        ))

        except Exception as e:
            print(f"  Error parsing {file_path}: {e}")
            return None

        if not messages:
            messages = event_messages
        if not messages:
            return None

        return UniversalSession(
            source_app=self.app_name,
            session_id=session_id or file_path.stem,
            project_slug=self.detect_project(file_path),
            model=model,
            started_at=first_ts,
            ended_at=last_ts,
            messages=messages,
            files_changed=sorted(files_changed),
        )
