"""Text chunking for embedding storage."""

from __future__ import annotations

from dataclasses import dataclass

from config import CHUNK_MAX_TOKENS, CHUNK_OVERLAP_TOKENS

# Rough token estimate: 1 token ≈ 4 chars (conservative for code)
CHARS_PER_TOKEN = 4


@dataclass
class Chunk:
    content: str
    line_start: int
    line_end: int
    token_count: int


def chunk_text(text: str) -> list[Chunk]:
    """Split text into overlapping chunks by line boundaries."""
    lines = text.split("\n")
    max_chars = CHUNK_MAX_TOKENS * CHARS_PER_TOKEN
    overlap_chars = CHUNK_OVERLAP_TOKENS * CHARS_PER_TOKEN

    chunks: list[Chunk] = []
    current_lines: list[str] = []
    current_chars = 0
    chunk_start_line = 0

    for i, line in enumerate(lines):
        line_len = len(line) + 1  # +1 for newline
        current_lines.append(line)
        current_chars += line_len

        if current_chars >= max_chars:
            content = "\n".join(current_lines)
            chunks.append(Chunk(
                content=content,
                line_start=chunk_start_line,
                line_end=i,
                token_count=len(content) // CHARS_PER_TOKEN,
            ))

            # Overlap: keep enough lines to cover overlap_chars
            overlap_lines: list[str] = []
            overlap_total = 0
            for prev_line in reversed(current_lines):
                overlap_total += len(prev_line) + 1
                overlap_lines.insert(0, prev_line)
                if overlap_total >= overlap_chars:
                    break

            current_lines = overlap_lines
            current_chars = sum(len(l) + 1 for l in current_lines)
            chunk_start_line = i - len(current_lines) + 1

    # Final chunk
    if current_lines:
        content = "\n".join(current_lines)
        if content.strip():
            chunks.append(Chunk(
                content=content,
                line_start=chunk_start_line,
                line_end=len(lines) - 1,
                token_count=len(content) // CHARS_PER_TOKEN,
            ))

    return chunks
