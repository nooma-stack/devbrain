"""DevBrain ingestion pipeline.

Processes transcript files: parse → store raw → chunk → embed → store chunks.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from adapters.base import UniversalSession
from adapters.claude_code import ClaudeCodeAdapter
from adapters.codex import CodexAdapter
from adapters.gemini import GeminiAdapter
from adapters.markdown_memory import MarkdownMemoryAdapter
from adapters.openclaw import OpenClawAdapter
from chunker import chunk_text
from db import delete_chunks_for_session, get_connection, get_or_create_project_id, get_existing_session_id, insert_chunk, insert_raw_session, session_exists, update_session_summary
from embeddings import embed, embed_batch

ADAPTERS = [ClaudeCodeAdapter(), OpenClawAdapter(), CodexAdapter(), GeminiAdapter(), MarkdownMemoryAdapter()]


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

    # Skip if exact same content already ingested (hash match)
    if not force and session_exists(adapter.app_name, fhash):
        return False

    print(f"  Parsing {path.name} with {adapter.app_name} adapter...")
    session = adapter.parse(path)
    if session is None:
        print(f"  Skipped (no messages or parse error)")
        return False

    # Check if this is an update to an existing session (same session_id, different hash)
    is_update = False
    if session.session_id:
        existing = get_existing_session_id(adapter.app_name, session.session_id)
        if existing:
            is_update = True
            print(f"  Updating existing session (content grew)")

    return _process_session(session, path, fhash, is_update=is_update)


def _embed_chunks(chunks) -> list[list[float]]:
    """Embed all chunk texts (batched, with per-text fallback) BEFORE the write
    transaction, so the slow Ollama calls hold no DB locks. A failed embed
    falls back to a zero vector so one bad chunk doesn't abort the whole
    session (a reembed pass backfills it)."""
    embeddings: list[list[float]] = []
    batch_size = 10
    for i in range(0, len(chunks), batch_size):
        texts = [c.content for c in chunks[i : i + batch_size]]
        try:
            embeddings.extend(embed_batch(texts))
        except Exception as e:  # noqa: BLE001
            print(f"  Embedding batch failed, falling back to individual: {e}")
            for text in texts:
                try:
                    embeddings.append(embed(text))
                except Exception:  # noqa: BLE001
                    embeddings.append([0.0] * 1024)
    return embeddings


def _process_session(session: UniversalSession, source_path: Path, source_hash: str, *, is_update: bool = False) -> bool:
    """Store raw session, chunk, embed, and store chunks."""
    # Resolve project — adapters only emit slugs from explicit mappings or
    # the auto-project-roots convention, so an unknown slug means a new
    # workspace folder: materialize it rather than orphaning the session.
    project_id = None
    if session.project_slug:
        project_id = get_or_create_project_id(session.project_slug)

    # Structured USF JSON for raw_content storage (structured access later)
    # Strip NUL bytes — PostgreSQL TEXT columns reject them
    raw_json = session.to_json().replace("\x00", "")

    # Plain text for chunking/embedding (embeddings work better on plain text)
    raw_text = session.to_text().replace("\x00", "")

    print(f"  {'Updating' if is_update else 'Storing'} raw session ({session.message_count} messages, {len(raw_json)} chars JSON)...")

    # Chunk + embed BEFORE opening the write transaction so the (slow) Ollama
    # calls hold no DB locks.
    chunks = chunk_text(raw_text)
    print(f"  Chunking: {len(chunks)} chunks")
    embeddings = _embed_chunks(chunks)

    # One transaction per session: raw_session + chunks + their memory
    # dual-writes commit together. A crash mid-ingest then leaves the session
    # atomically ABSENT (the source-hash gate re-ingests it cleanly on the next
    # scan) instead of committing raw_session first and stranding a
    # half-indexed session the hash gate would never re-complete.
    session_db_id = ""
    total_stored = 0
    with get_connection() as conn, conn.cursor() as cur:
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
            raw_content=raw_json,
            summary=None,  # Summarization handled separately
            files_touched=session.files_changed,
            cur=cur,
        )
        if not session_db_id:
            print(f"  Already exists (hash collision), skipping.")
            return False
        # If updating, delete old chunks so we re-embed the full session.
        if is_update:
            delete_chunks_for_session(session_db_id, cur=cur)
            print(f"  Cleared old chunks for re-embedding")
        for chunk, emb in zip(chunks, embeddings):
            insert_chunk(
                project_id=project_id,
                source_type="session",
                source_id=session_db_id,
                source_line_start=chunk.line_start,
                source_line_end=chunk.line_end,
                content=chunk.content,
                embedding=emb,
                token_count=chunk.token_count,
                cur=cur,
            )
            total_stored += 1
        conn.commit()

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
            # Chunk the summary to stay within embedding model token limits
            summary_chunks = chunk_text(summary)
            texts = [c.content for c in summary_chunks]
            try:
                embeddings = embed_batch(texts)
            except Exception:
                embeddings = [embed(t) for t in texts]
            for chunk, emb in zip(summary_chunks, embeddings):
                insert_chunk(
                    project_id=project_id,
                    source_type="session_summary",
                    source_id=session_db_id,
                    source_line_start=chunk.line_start,
                    source_line_end=chunk.line_end,
                    content=chunk.content,
                    embedding=emb,
                    token_count=chunk.token_count,
                )
            print(f"  Summary stored ({len(summary)} chars, {len(summary_chunks)} chunk{'s' if len(summary_chunks) > 1 else ''})")
    except Exception as e:
        print(f"  Summarization failed (non-blocking): {e}")
