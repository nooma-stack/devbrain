"""Codex CLI transcript adapter.

Parses ~/.codex/sessions/**/*.jsonl session files into Universal Session Format.

Codex JSONL format:
- type=session_meta: {id, timestamp, cwd, cli_version, model_provider, ...}
- type=response_item: {type: "message", role: "user"|"assistant"|"developer", content: [{type, text}]}
- type=event_msg: {type, turn_id, ...}
- type=turn_context: {type, ...}
"""

from __future__ import annotations

import json
from pathlib import Path

from .base import UniversalMessage, UniversalSession


class CodexAdapter:
    app_name = "codex"
    file_patterns = ["*.jsonl"]

    def detect(self, file_path: Path) -> bool:
        return file_path.suffix == ".jsonl" and ".codex" in str(file_path)

    def detect_project(self, file_path: Path) -> str | None:
        """Infer project from session_meta cwd field."""
        try:
            with open(file_path, encoding="utf-8") as f:
                for line in f:
                    entry = json.loads(line.strip())
                    if entry.get("type") == "session_meta":
                        cwd = entry.get("payload", {}).get("cwd", "")
                        path_to_slug = {
                            "/Users/patrickkelly/Developer/lighthouse/brightbot": "brightbot",
                            "/Users/patrickkelly/pkrelay": "pkrelay",
                            "/Users/patrickkelly/devbrain": "devbrain",
                        }
                        for path, slug in path_to_slug.items():
                            if cwd.startswith(path):
                                return slug
                        return None
        except Exception:
            pass
        return None

    def parse(self, file_path: Path) -> UniversalSession | None:
        """Parse a Codex CLI JSONL session file."""
        messages: list[UniversalMessage] = []
        session_id: str | None = None
        model: str | None = None
        first_ts: str | None = None
        last_ts: str | None = None
        files_changed: set[str] = set()

        try:
            with open(file_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    ts = entry.get("timestamp")
                    if ts:
                        if first_ts is None:
                            first_ts = ts
                        last_ts = ts

                    entry_type = entry.get("type", "")
                    payload = entry.get("payload", {})

                    if entry_type == "session_meta":
                        session_id = payload.get("id")
                        model = payload.get("model_provider")

                    elif entry_type == "response_item" and isinstance(payload, dict):
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
                                        content_parts.append(f"[tool_result] {str(output)[:500]}")
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

        except Exception as e:
            print(f"  Error parsing {file_path}: {e}")
            return None

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
