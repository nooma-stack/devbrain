"""Base adapter interface."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class UniversalMessage:
    role: str
    timestamp: str | None = None
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)


@dataclass
class UniversalSession:
    source_app: str
    session_id: str | None = None
    project_slug: str | None = None
    model: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    messages: list[UniversalMessage] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)

    @property
    def message_count(self) -> int:
        return len(self.messages)

    def to_text(self) -> str:
        """Convert to plain text for chunking/embedding."""
        lines: list[str] = []
        for msg in self.messages:
            prefix = f"[{msg.role}]"
            if msg.timestamp:
                prefix = f"[{msg.timestamp}] [{msg.role}]"
            lines.append(f"{prefix} {msg.content[:2000]}")
        return "\n".join(lines)

    def to_json(self) -> str:
        """Serialize to structured USF JSON format."""
        return json.dumps(
            {
                "usf_version": "1.0",
                "source_app": self.source_app,
                "session_id": self.session_id,
                "project": self.project_slug,
                "model": self.model,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "message_count": self.message_count,
                "files_changed": self.files_changed,
                "messages": [
                    {
                        "role": msg.role,
                        "timestamp": msg.timestamp,
                        "content": msg.content,
                        "tool_calls": msg.tool_calls,
                        "files_touched": msg.files_touched,
                    }
                    for msg in self.messages
                ],
            },
            ensure_ascii=False,
        )


class TranscriptAdapter(Protocol):
    app_name: str
    file_patterns: list[str]

    def detect(self, file_path: Path) -> bool: ...
    def parse(self, file_path: Path) -> UniversalSession | None: ...
    def detect_project(self, file_path: Path) -> str | None: ...
