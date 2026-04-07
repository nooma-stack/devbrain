"""Base adapter interface."""

from __future__ import annotations

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


class TranscriptAdapter(Protocol):
    app_name: str
    file_patterns: list[str]

    def detect(self, file_path: Path) -> bool: ...
    def parse(self, file_path: Path) -> UniversalSession | None: ...
    def detect_project(self, file_path: Path) -> str | None: ...
