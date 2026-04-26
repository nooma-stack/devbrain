"""Factory learning loop.

Extracts generalizable lessons from past review findings and stores them
as patterns in DevBrain. These patterns are injected into future planning
prompts to avoid repeating the same mistakes.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from pathlib import Path

import psycopg2

logger = logging.getLogger(__name__)

# Load config (env > yaml > defaults precedence; see factory/config.py)
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
# ingest/ is a sibling of factory/ — extend sys.path so memory_writer
# (the canonical Python dual-write helper for P2.b) is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ingest"))
from config import DATABASE_URL, SUMMARIZE_MODEL, load_config  # noqa: E402
from memory_writer import record_memory  # noqa: E402

_config = load_config()
OLLAMA_URL = _config.get("embedding", {}).get("url", _config["summarization"]["url"])
EMBED_MODEL = _config.get("embedding", {}).get("model", "snowflake-arctic-embed2")

EXTRACTION_PROMPT = """You are analyzing code review findings from an automated dev factory pipeline. Extract generalizable lessons that would help future planning avoid the same issues.

REVIEW FINDINGS:
{findings}

PROJECT: {project_slug}
FEATURE: {feature_title}

For each finding, determine:
1. Is this a generalizable lesson (applies beyond this specific feature)?
2. If yes, write a concise lesson in this format:
   - LESSON: [one sentence describing what to do or avoid]
   - CATEGORY: [one of: security, hipaa, architecture, testing, performance, code-quality]
   - CONTEXT: [when this lesson applies]

Only extract lessons that are actionable and generalizable. Skip findings that are purely feature-specific.

Output ONLY the lessons in the format above, one per finding. If no generalizable lessons exist, output "NO_LESSONS"."""


def _embed(text: str) -> list[float]:
    data = json.dumps({"model": EMBED_MODEL, "input": text}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    return result["embeddings"][0]


def _summarize(prompt: str) -> str:
    data = json.dumps({
        "model": SUMMARIZE_MODEL,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 1024},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    return result["response"].strip()


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def extract_lessons(job_id: str) -> list[dict]:
    """Extract generalizable lessons from a completed factory job's review findings.

    Returns list of extracted lessons with their embeddings.
    """
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            # Get job info
            cur.execute(
                """SELECT j.title, p.slug, p.id
                   FROM devbrain.factory_jobs j
                   JOIN devbrain.projects p ON j.project_id = p.id
                   WHERE j.id = %s""",
                (job_id,),
            )
            row = cur.fetchone()
            if not row:
                logger.warning("Job %s not found", job_id[:8])
                return []
            feature_title, project_slug, project_id = row

            # Get all review artifacts with blocking findings
            cur.execute(
                """SELECT content FROM devbrain.factory_artifacts
                   WHERE job_id = %s AND phase = 'review' AND blocking_count > 0
                   ORDER BY created_at""",
                (job_id,),
            )
            findings = [r[0] for r in cur.fetchall()]

        if not findings:
            logger.info("No blocking findings for job %s — nothing to learn", job_id[:8])
            return []

        # Ask LLM to extract generalizable lessons
        combined = "\n\n---\n\n".join(findings)
        prompt = EXTRACTION_PROMPT.format(
            findings=combined[:8000],
            project_slug=project_slug,
            feature_title=feature_title,
        )

        logger.info("Extracting lessons from %d review artifacts...", len(findings))
        response = _summarize(prompt)

        if "NO_LESSONS" in response:
            logger.info("No generalizable lessons found")
            return []

        # Parse lessons
        lessons = _parse_lessons(response)
        logger.info("Extracted %d lessons", len(lessons))

        # Deduplicate against existing patterns
        stored = _store_lessons(conn, lessons, project_id, job_id)
        logger.info("Stored %d new lessons (deduplicated)", len(stored))

        return stored

    finally:
        conn.close()


def _parse_lessons(response: str) -> list[dict]:
    """Parse LLM response into structured lessons."""
    lessons = []
    current = {}

    for line in response.split("\n"):
        line = line.strip()
        if not line:
            continue

        if line.upper().startswith("- LESSON:") or line.upper().startswith("LESSON:"):
            if current.get("lesson"):
                lessons.append(current)
            current = {"lesson": line.split(":", 1)[1].strip()}
        elif line.upper().startswith("- CATEGORY:") or line.upper().startswith("CATEGORY:"):
            current["category"] = line.split(":", 1)[1].strip().lower()
        elif line.upper().startswith("- CONTEXT:") or line.upper().startswith("CONTEXT:"):
            current["context"] = line.split(":", 1)[1].strip()

    if current.get("lesson"):
        lessons.append(current)

    return lessons


def _store_lessons(
    conn, lessons: list[dict], project_id: str, job_id: str
) -> list[dict]:
    """Store lessons as patterns, deduplicating by semantic similarity."""
    stored = []

    # P2.d.i read switch: existing-pattern dedup reads embeddings from
    # devbrain.memory (kind='pattern') instead of the chunks JOIN
    # patterns shape used pre-switch. The applies_when filter is the
    # canonical Phase-3 marker; until the curator populates it the
    # content-LIKE fallback finds factory_review lessons by their
    # serialized "Context: …" tail.
    with conn.cursor() as cur:
        cur.execute(
            """SELECT embedding::text, content
               FROM devbrain.memory
               WHERE project_id = %s
                 AND kind = 'pattern'
                 AND archived_at IS NULL
                 AND embedding IS NOT NULL
                 AND (applies_when->>'category' = 'factory_review'
                      OR content LIKE %s)""",
            (project_id, "%Context: %"),
        )
        existing = []
        for row in cur.fetchall():
            vec_str = row[0].strip("[]")
            if vec_str:
                existing.append({
                    "embedding": [float(x) for x in vec_str.split(",")],
                    "content": row[1],
                })

        # Dual-write drift detector: if memory returned nothing but the
        # legacy patterns table has factory_review rows, the dual-write
        # has fallen behind for this project. Surface as WARNING so
        # operators can rerun backfill before P2.d.ii drops legacy.
        if not existing:
            cur.execute(
                """SELECT 1 FROM devbrain.patterns
                   WHERE project_id = %s AND category = 'factory_review'
                   LIMIT 1""",
                (project_id,),
            )
            if cur.fetchone() is not None:
                logger.warning(
                    "dual-write drift: devbrain.memory returned 0 "
                    "factory_review patterns for project %s but legacy "
                    "devbrain.patterns has rows — run backfill-memory",
                    project_id,
                )

    for lesson in lessons:
        text = lesson["lesson"]
        category = lesson.get("category", "general")
        context = lesson.get("context", "")

        # Embed the lesson
        embedding = _embed(text)

        # Check similarity against existing patterns
        is_duplicate = False
        for ex in existing:
            sim = _cosine_similarity(embedding, ex["embedding"])
            if sim > 0.85:
                logger.debug("Skipping duplicate lesson (sim=%.3f): %s", sim, text[:80])
                is_duplicate = True
                break

        if is_duplicate:
            continue

        # Store as pattern + chunk
        vector_str = f"[{','.join(str(x) for x in embedding)}]"

        description = f"{text}\n\nContext: {context}"
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO devbrain.patterns
                       (project_id, name, category, description, tags)
                   VALUES (%s, %s, %s, %s, %s)
                   RETURNING id""",
                (
                    project_id,
                    text[:100],
                    "factory_review",
                    description,
                    json.dumps([category, "factory_learning"]),
                ),
            )
            pattern_id = str(cur.fetchone()[0])

            # P2.b dual-write: pattern row → devbrain.memory. We dual-write
            # the pattern (not the auxiliary chunk insert below) so each
            # logical lesson lands as exactly one memory row. SAVEPOINT
            # inside record_memory keeps a memory failure from rolling
            # back the pattern + chunk legacy commit.
            record_memory(
                cur,
                project_id=project_id,
                kind="pattern",
                content=description,
                title=text[:100],
                embedding_sql=vector_str,
                provenance_id=pattern_id,
            )

            cur.execute(
                """INSERT INTO devbrain.chunks
                       (project_id, source_type, source_id, content, embedding, metadata)
                   VALUES (%s, 'pattern', %s, %s, %s::vector, %s)""",
                (
                    project_id,
                    pattern_id,
                    description,
                    vector_str,
                    json.dumps({
                        "category": category,
                        "source_job": job_id,
                        "type": "factory_learning",
                    }),
                ),
            )

        conn.commit()
        stored.append(lesson)
        existing.append({"embedding": embedding, "content": text})

    return stored


def get_review_lessons(project_id: str, limit: int = 10) -> list[str]:
    """Get stored review lessons for a project, for injection into planning prompts.

    P2.d.i: reads from devbrain.memory (kind='pattern') instead of the
    legacy patterns table. applies_when->>'category' is the canonical
    Phase-3 marker; the content-LIKE fallback bridges until the curator
    populates applies_when on every row. Returns content from the
    memory row (P2.b dual-write writes the same description).
    """
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT content
                   FROM devbrain.memory
                   WHERE project_id = %s
                     AND kind = 'pattern'
                     AND archived_at IS NULL
                     AND (applies_when->>'category' = 'factory_review'
                          OR content LIKE %s)
                   ORDER BY created_at DESC
                   LIMIT %s""",
                (project_id, "%Context: %", limit),
            )
            results = [row[0] for row in cur.fetchall()]

            # Dual-write drift detector — see _store_lessons for context.
            if not results:
                cur.execute(
                    """SELECT 1 FROM devbrain.patterns
                       WHERE project_id = %s AND category = 'factory_review'
                       LIMIT 1""",
                    (project_id,),
                )
                if cur.fetchone() is not None:
                    logger.warning(
                        "dual-write drift: devbrain.memory returned 0 "
                        "factory_review lessons for project %s but legacy "
                        "devbrain.patterns has rows — run backfill-memory",
                        project_id,
                    )

            return results
    finally:
        conn.close()
