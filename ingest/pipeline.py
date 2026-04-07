"""DevBrain ingestion pipeline.

Processes transcript files: parse → store raw → chunk → embed → store chunks.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from adapters.base import UniversalSession
from adapters.claude_code import ClaudeCodeAdapter
from adapters.openclaw import OpenClawAdapter
from chunker import chunk_text
from db import get_project_id, insert_chunk, insert_raw_session, session_exists, update_session_summary
from embeddings import embed, embed_batch

ADAPTERS = [ClaudeCodeAdapter(), OpenClawAdapter()]


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def detect_adapter(path: Path):
    for adapter in ADAPTERS:
        if adapter.detect(path):
            return adapter
    return None


def ingest_file(path: Path, *, force: bool = False) -> bool:
    """Ingest a single session file. Returns True if processed."""
    adapter = detect_adapter(path)
    if adapter is None:
        return False

    fhash = file_hash(path)

    # Skip if already ingested (unless forced)
    if not force and session_exists(adapter.app_name, fhash):
        return False

    print(f"  Parsing {path.name} with {adapter.app_name} adapter...")
    session = adapter.parse(path)
    if session is None:
        print(f"  Skipped (no messages or parse error)")
        return False

    return _process_session(session, path, fhash)


def _process_session(session: UniversalSession, source_path: Path, source_hash: str) -> bool:
    """Store raw session, chunk, embed, and store chunks."""
    # Resolve project
    project_id = None
    if session.project_slug:
        project_id = get_project_id(session.project_slug)

    # Convert to text for storage and chunking
    raw_text = session.to_text()

    print(f"  Storing raw session ({session.message_count} messages, {len(raw_text)} chars)...")

    # Store raw session
    session_db_id = insert_raw_session(
        project_id=project_id,
        source_app=session.source_app,
        source_path=str(source_path),
        source_hash=source_hash,
        session_id=session.session_id,
        model_used=session.model,
        started_at=session.started_at,
        ended_at=session.ended_at,
        message_count=session.message_count,
        raw_content=raw_text,
        summary=None,  # Summarization handled separately
        files_touched=session.files_changed,
    )

    if not session_db_id:
        print(f"  Already exists (hash collision), skipping.")
        return False

    # Chunk the text
    chunks = chunk_text(raw_text)
    print(f"  Chunking: {len(chunks)} chunks")

    if not chunks:
        return True

    # Embed chunks in batches
    batch_size = 10
    total_stored = 0

    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        texts = [c.content for c in batch]

        try:
            embeddings = embed_batch(texts)
        except Exception as e:
            print(f"  Embedding batch failed, falling back to individual: {e}")
            embeddings = []
            for text in texts:
                try:
                    embeddings.append(embed(text))
                except Exception:
                    embeddings.append([0.0] * 1024)

        for chunk, emb in zip(batch, embeddings):
            insert_chunk(
                project_id=project_id,
                source_type="session",
                source_id=session_db_id,
                source_line_start=chunk.line_start,
                source_line_end=chunk.line_end,
                content=chunk.content,
                embedding=emb,
                token_count=chunk.token_count,
            )
            total_stored += 1

    print(f"  Stored {total_stored} embedded chunks")

    # Auto-summarize using local LLM
    _summarize_session(session_db_id, raw_text, project_id)

    return True


def _summarize_session(session_db_id: str, raw_text: str, project_id: str | None) -> None:
    """Generate and store a summary using local Ollama model."""
    try:
        from summarize import summarize_text
        print("  Summarizing...")
        summary = summarize_text(raw_text)
        if summary:
            update_session_summary(session_db_id, summary)
            # Also store summary as its own searchable chunk
            summary_embedding = embed(summary)
            insert_chunk(
                project_id=project_id,
                source_type="session_summary",
                source_id=session_db_id,
                source_line_start=None,
                source_line_end=None,
                content=summary,
                embedding=summary_embedding,
                token_count=len(summary) // 4,
            )
            print(f"  Summary stored ({len(summary)} chars)")
    except Exception as e:
        print(f"  Summarization failed (non-blocking): {e}")
