"""Gemini CLI transcript adapter.

Parses ~/.gemini/tmp/*/chats/session-*.json files into Universal Session Format.

Gemini JSON format:
{
  "sessionId": "...",
  "projectHash": "...",
  "startTime": "2026-03-23T22:40:05.290Z",
  "lastUpdated": "2026-03-23T22:40:46.509Z",
  "messages": [
    {"id": "...", "timestamp": "...", "type": "user"|"gemini", "content": "..." | [{type, text}]}
  ]
}
"""

from __future__ import annotations

import json
from pathlib import Path

from config import ADAPTER_CONFIG

from .base import UniversalMessage, UniversalSession


class GeminiAdapter:
    app_name = "gemini"
    file_patterns = ["session-*.json"]

    def detect(self, file_path: Path) -> bool:
        return (
            file_path.suffix == ".json"
            and file_path.name.startswith("session-")
            and ".gemini" in str(file_path)
        )

    def detect_project(self, file_path: Path) -> str | None:
        """Infer project from directory structure under ~/.gemini/tmp/.

        Lookup order:
          1. Exact match against ingest.adapters.gemini.dir_to_project
          2. Substring match against ingest.adapters.gemini.project_keywords
             ({slug: [keyword, ...]} — case-insensitive on dir name)
        """
        cfg = ADAPTER_CONFIG.get("gemini", {})
        dir_to_project: dict[str, str] = cfg.get("dir_to_project", {}) or {}
        project_keywords: dict[str, list[str]] = cfg.get("project_keywords", {}) or {}

        parts = file_path.parts
        try:
            tmp_idx = parts.index("tmp")
            if tmp_idx + 1 < len(parts):
                project_dir = parts[tmp_idx + 1]
                if project_dir in dir_to_project:
                    return dir_to_project[project_dir]
                lower = project_dir.lower()
                for slug, keywords in project_keywords.items():
                    if any(kw.lower() in lower for kw in keywords):
                        return slug
        except (ValueError, IndexError):
            pass
        return None

    def parse(self, file_path: Path) -> UniversalSession | None:
        """Parse a Gemini CLI session JSON file."""
        try:
            with open(file_path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"  Error reading {file_path}: {e}")
            return None

        raw_messages = data.get("messages", [])
        if not raw_messages:
            return None

        messages: list[UniversalMessage] = []

        for msg in raw_messages:
            msg_type = msg.get("type", "")
            ts = msg.get("timestamp")

            # Map gemini roles to standard roles
            if msg_type == "user":
                role = "user"
            elif msg_type == "gemini":
                role = "assistant"
            else:
                continue

            # Extract content
            content_parts: list[str] = []
            raw_content = msg.get("content", "")

            if isinstance(raw_content, str):
                content_parts.append(raw_content)
            elif isinstance(raw_content, list):
                for block in raw_content:
                    if isinstance(block, dict):
                        text = block.get("text", "")
                        if text:
                            content_parts.append(text)
                    elif isinstance(block, str):
                        content_parts.append(block)

            content = "\n".join(content_parts).strip()
            if content:
                messages.append(UniversalMessage(
                    role=role,
                    timestamp=ts,
                    content=content,
                ))

        if not messages:
            return None

        return UniversalSession(
            source_app=self.app_name,
            session_id=data.get("sessionId", file_path.stem),
            project_slug=self.detect_project(file_path),
            model="gemini",
            started_at=data.get("startTime"),
            ended_at=data.get("lastUpdated"),
            messages=messages,
            files_changed=[],
        )
