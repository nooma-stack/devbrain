"""Markdown memory file adapter.

Parses project memory/*.md files (like BrightBot's per-date development notes)
into Universal Session Format for ingestion into DevBrain.
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import UniversalMessage, UniversalSession


class MarkdownMemoryAdapter:
    app_name = "markdown_memory"
    file_patterns = ["*.md"]

    # Map known memory directories to project slugs
    MEMORY_DIRS = {
        "/Users/patrickkelly/Developer/lighthouse/brightbot/memory": "brightbot",
    }

    def detect(self, file_path: Path) -> bool:
        if file_path.suffix != ".md":
            return False
        return any(str(file_path).startswith(d) for d in self.MEMORY_DIRS)

    def detect_project(self, file_path: Path) -> str | None:
        for dir_path, slug in self.MEMORY_DIRS.items():
            if str(file_path).startswith(dir_path):
                return slug
        return None

    def parse(self, file_path: Path) -> UniversalSession | None:
        """Parse a markdown memory file as a single-message session."""
        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception:
            return None

        if not content.strip():
            return None

        # Extract date from filename if present (e.g., 2026-03-23.md)
        date_match = re.match(r"(\d{4}-\d{2}-\d{2})", file_path.stem)
        timestamp = f"{date_match.group(1)}T00:00:00Z" if date_match else None

        return UniversalSession(
            source_app=self.app_name,
            session_id=file_path.stem,
            project_slug=self.detect_project(file_path),
            model=None,
            started_at=timestamp,
            ended_at=timestamp,
            messages=[
                UniversalMessage(
                    role="assistant",
                    timestamp=timestamp,
                    content=content,
                )
            ],
            files_changed=[],
        )
