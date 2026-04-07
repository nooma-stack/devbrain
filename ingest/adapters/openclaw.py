"""OpenClaw transcript adapter.

Parses ~/.openclaw/agents/*/sessions/*.jsonl session files into Universal Session Format.
"""

from __future__ import annotations

import json
from pathlib import Path

from .base import UniversalMessage, UniversalSession


class OpenClawAdapter:
    app_name = "openclaw"
    file_patterns = ["*.jsonl"]

    def detect(self, file_path: Path) -> bool:
        return file_path.suffix == ".jsonl" and ".openclaw" in str(file_path)

    def detect_project(self, file_path: Path) -> str | None:
        """OpenClaw agents work in configured workspaces.

        Default workspace is brightbot. Agent name can hint at project.
        """
        # Check if we can determine from the agent config
        parts = file_path.parts
        try:
            agents_idx = parts.index("agents")
            if agents_idx + 1 < len(parts):
                agent_name = parts[agents_idx + 1]
                # All OpenClaw agents default to brightbot workspace
                return "brightbot"
        except (ValueError, IndexError):
            pass
        return "brightbot"

    def parse(self, file_path: Path) -> UniversalSession | None:
        """Parse an OpenClaw JSONL session file."""
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

                    if entry.get("type") != "message":
                        continue

                    msg_data = entry.get("message", {})
                    role = msg_data.get("role", "unknown")
                    ts = entry.get("timestamp")
                    model = msg_data.get("model")

                    if ts:
                        if first_ts is None:
                            first_ts = ts
                        last_ts = ts

                    if model:
                        models_seen.add(model)

                    # Extract text content
                    content_parts: list[str] = []
                    tool_calls: list[dict] = []

                    raw_content = msg_data.get("content", "")
                    if isinstance(raw_content, str):
                        content_parts.append(raw_content)
                    elif isinstance(raw_content, list):
                        for block in raw_content:
                            if isinstance(block, dict):
                                block_type = block.get("type", "")
                                if block_type == "text":
                                    content_parts.append(block.get("text", ""))
                                elif block_type == "thinking":
                                    # Include thinking content for context
                                    thinking = block.get("thinking", "")
                                    if thinking:
                                        content_parts.append(f"[thinking] {thinking[:1000]}")
                                elif block_type in ("tool_use", "function_call"):
                                    name = block.get("name", block.get("function", {}).get("name", ""))
                                    args = block.get("input", block.get("arguments", {}))
                                    tool_calls.append({"tool": name, "args": args})
                                    # Track file paths
                                    if isinstance(args, dict):
                                        for key in ("file_path", "path", "file", "target"):
                                            val = args.get(key)
                                            if isinstance(val, str) and "/" in val:
                                                files_changed.add(val)

                    content = "\n".join(content_parts).strip()
                    if content or tool_calls:
                        messages.append(UniversalMessage(
                            role=role,
                            timestamp=ts,
                            content=content,
                            tool_calls=tool_calls,
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
