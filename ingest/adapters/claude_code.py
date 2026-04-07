"""Claude Code transcript adapter.

Parses ~/.claude/projects/*/*.jsonl session files into Universal Session Format.
"""

from __future__ import annotations

import json
from pathlib import Path

from .base import TranscriptAdapter, UniversalMessage, UniversalSession


class ClaudeCodeAdapter:
    app_name = "claude_code"
    file_patterns = ["*.jsonl"]

    def detect(self, file_path: Path) -> bool:
        return file_path.suffix == ".jsonl" and ".claude" in str(file_path)

    def detect_project(self, file_path: Path) -> str | None:
        """Infer project from the directory structure.

        Claude Code stores sessions in ~/.claude/projects/-path-to-project/
        where the path uses dashes instead of slashes.
        """
        parts = file_path.parts
        try:
            proj_idx = parts.index("projects")
            if proj_idx + 1 < len(parts):
                encoded_path = parts[proj_idx + 1]
                # Decode: -Users-patrickkelly-Developer-lighthouse-brightbot
                # → /Users/patrickkelly/Developer/lighthouse/brightbot
                decoded = "/" + encoded_path.lstrip("-").replace("-", "/")
                # Map known paths to slugs
                path_to_slug = {
                    "/Users/patrickkelly/Developer/lighthouse/brightbot": "brightbot",
                    "/Users/patrickkelly/pkrelay": "pkrelay",
                    "/Users/patrickkelly/devbrain": "devbrain",
                    "/Users/patrickkelly": None,  # home dir sessions = no specific project
                }
                # Try exact match first
                if decoded in path_to_slug:
                    return path_to_slug[decoded]
                # Try partial match
                for path, slug in path_to_slug.items():
                    if decoded.startswith(path) and slug:
                        return slug
        except (ValueError, IndexError):
            pass
        return None

    def parse(self, file_path: Path) -> UniversalSession | None:
        """Parse a Claude Code JSONL session file."""
        messages: list[UniversalMessage] = []
        models_seen: set[str] = set()
        files_changed: set[str] = set()
        first_ts: str | None = None
        last_ts: str | None = None
        session_id = file_path.stem

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

                    entry_type = entry.get("type", "")
                    if entry_type not in ("user", "assistant", "message"):
                        continue

                    msg_data = entry.get("message", {})
                    if not isinstance(msg_data, dict):
                        continue

                    # Claude Code uses type=user/assistant with message={role, content}
                    # Some formats use type=message with message={role, content}
                    role = msg_data.get("role", entry_type)
                    ts = entry.get("timestamp")
                    model = msg_data.get("model") or entry.get("model")

                    if ts:
                        if first_ts is None:
                            first_ts = ts
                        last_ts = ts

                    if model:
                        models_seen.add(model)

                    # Extract text content
                    content_parts: list[str] = []
                    tool_calls: list[dict] = []
                    msg_files: list[str] = []

                    raw_content = msg_data.get("content", "")
                    if isinstance(raw_content, str):
                        content_parts.append(raw_content)
                    elif isinstance(raw_content, list):
                        for block in raw_content:
                            if isinstance(block, dict):
                                if block.get("type") == "text":
                                    content_parts.append(block.get("text", ""))
                                elif block.get("type") == "tool_use":
                                    tool_name = block.get("name", "")
                                    tool_input = block.get("input", {})
                                    tool_calls.append({
                                        "tool": tool_name,
                                        "args": tool_input,
                                    })
                                    # Track file paths from tool calls
                                    if isinstance(tool_input, dict):
                                        for key in ("file_path", "path", "file"):
                                            if key in tool_input and isinstance(tool_input[key], str):
                                                msg_files.append(tool_input[key])
                                                files_changed.add(tool_input[key])
                                elif block.get("type") == "tool_result":
                                    result_text = block.get("content", "")
                                    if isinstance(result_text, str):
                                        content_parts.append(f"[tool_result] {result_text[:500]}")

                    content = "\n".join(content_parts).strip()
                    if content or tool_calls:
                        messages.append(UniversalMessage(
                            role=role,
                            timestamp=ts,
                            content=content,
                            tool_calls=tool_calls,
                            files_touched=msg_files,
                        ))
        except Exception as e:
            print(f"  Error parsing {file_path}: {e}")
            return None

        if not messages:
            return None

        return UniversalSession(
            source_app=self.app_name,
            session_id=session_id,
            project_slug=self.detect_project(file_path),
            model=sorted(models_seen)[0] if models_seen else None,
            started_at=first_ts,
            ended_at=last_ts,
            messages=messages,
            files_changed=sorted(files_changed),
        )
