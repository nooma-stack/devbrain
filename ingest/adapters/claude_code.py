"""Claude Code transcript adapter.

Parses ~/.claude/projects/*/*.jsonl session files into Universal Session Format.
"""

from __future__ import annotations

import json
from pathlib import Path

import re
from fnmatch import fnmatch

from config import AUTO_PROJECT_ROOTS, PROJECT_MAPPINGS

from .base import TranscriptAdapter, UniversalMessage, UniversalSession


def _auto_root_match(absolute_path: str) -> tuple[str | None, int]:
    """(slug, number of path segments in the matched root pattern) for the
    auto-project-root convention; (None, 0) when no root matches."""
    segs = Path(absolute_path).parts
    for pattern in AUTO_PROJECT_ROOTS:
        pat_segs = Path(pattern).parts
        if len(segs) <= len(pat_segs):
            continue
        if all(fnmatch(s, p) for s, p in zip(segs, pat_segs)):
            slug = segs[len(pat_segs)].lower()
            slug = re.sub(r"[^a-z0-9_-]+", "-", slug).strip("-")
            return (slug or None), len(pat_segs)
    return None, 0


def _auto_root_slug(absolute_path: str) -> str | None:
    """Derive a project slug from a workspace-root convention.

    AUTO_PROJECT_ROOTS holds glob patterns like "/Users/*/lighthouse".
    When a session path sits under a matching root, the next path
    segment names the project (lighthouse/brightbot/... -> brightbot),
    auto-created downstream on first sight. Convention: project folders
    directly under a root should avoid dashes — the Claude Code
    transcript-dir encoding turns dashes into slashes, which would split
    the folder name before it reaches us.
    """
    return _auto_root_match(absolute_path)[0]


def _lookup_slug(absolute_path: str) -> str | None:
    """Map an absolute filesystem path to a project slug via config.

    Tries exact match first, then longest-prefix match, then the
    auto-project-root convention. Returns None when nothing applies
    (sessions not tied to a configured project).
    """
    expanded = {
        str(Path(k).expanduser()): v for k, v in PROJECT_MAPPINGS.items()
    }
    if absolute_path in expanded:
        return expanded[absolute_path]
    matches = [
        (path, slug) for path, slug in expanded.items()
        if absolute_path.startswith(path) and slug
    ]
    matches.sort(key=lambda x: len(x[0]), reverse=True)  # longest prefix wins
    auto_slug, auto_depth = _auto_root_match(absolute_path)
    if matches:
        best_path, best_slug = matches[0]
        # A prefix mapping only beats the auto-root convention when it is
        # more specific than the root itself. Otherwise a mapping for a
        # bare workspace root (e.g. ~/Trifekta -> some-project, meant for
        # sessions started in the root folder) would swallow every
        # <root>/<folder> session the convention should name <folder>.
        if auto_slug and len(Path(best_path).parts) <= auto_depth:
            return auto_slug
        return best_slug
    return auto_slug


class ClaudeCodeAdapter:
    app_name = "claude_code"
    file_patterns = ["*.jsonl"]

    def detect(self, file_path: Path) -> bool:
        s = str(file_path)
        # ".claude" for local sessions; "ingest-incoming" for remote-dev
        # transcripts shipped to the drop zone (whose path has no .claude).
        return file_path.suffix == ".jsonl" and (
            ".claude" in s or "ingest-incoming" in s
        )

    def detect_project(self, file_path: Path) -> str | None:
        """Infer project from the encoded-path directory.

        Claude Code stores sessions in ~/.claude/projects/-path-to-project/
        where the path uses dashes instead of slashes.
        """
        parts = file_path.parts
        try:
            proj_idx = parts.index("projects")
            if proj_idx + 1 < len(parts):
                encoded_path = parts[proj_idx + 1]
                decoded = "/" + encoded_path.lstrip("-").replace("-", "/")
                return _lookup_slug(decoded)
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
                    # Typed by the person: a user entry that is not harness
                    # text (isMeta: skills, caveats, injected reminders) and
                    # carries no tool result. Injection screening trusts
                    # nothing else (and still cuts at the first harness
                    # marker inside the text).
                    has_tool_result = isinstance(raw_content, list) and any(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in raw_content
                    )
                    principal = (
                        role == "user"
                        and entry_type == "user"
                        and not entry.get("isMeta")
                        and not entry.get("isCompactSummary")
                        and not has_tool_result
                        and bool(content)
                    )
                    if content or tool_calls:
                        messages.append(UniversalMessage(
                            role=role,
                            timestamp=ts,
                            content=content,
                            tool_calls=tool_calls,
                            files_touched=msg_files,
                            principal=principal,
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
