"""cognify_edges — auto-infer cites and contradicts edges between memory rows.

Two edge types are detected:
  - cites: narrative cross-references. Detected via text pattern matching
    (zero LLM cost, deterministic).
  - contradicts: semantic conflicts. LLM-judged pair comparison. Pair
    selection uses Phase 5's graph_walk (factory.graph.walker.walk) to find
    candidates within K hops — near-graph pairs are more likely to be related.

LLM cost ceiling: max 15 calls per pass for contradicts detection.
6-hour cadence via launchd.

Phase 5 dependency: walk() is called directly. Phase 5 is live in main
(commit 9523987). No fallback needed.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

from cognify.orchestrator import CognifyPass, PassResult, register_pass
from graph.walker import walk

logger = logging.getLogger(__name__)

# Max LLM calls per pass for contradicts detection.
MAX_LLM_CALLS_PER_PASS = 15

# Walk parameters for contradiction candidate selection.
# §4.3 of Phase 6 design specifies max_hops=2 for candidate selection.
CONTRADICTION_MAX_HOPS = 2
CONTRADICTION_MAX_NODES = 50

# Tiers whose rows are candidates for contradiction checking.
# Lessons and rules carry semantic claims; decisions are also worth checking.
CONTRADICTION_SEED_TIERS = frozenset(["lesson", "rule"])

# Edge types used when walking for contradiction candidate pairs.
# Per Phase 6 §4.3: derived_from, refined_by, depends_on (not 'cites' — we're
# looking for related knowledge, not just textual references).
CONTRADICTION_WALK_EDGE_TYPES = ["derived_from", "refined_by", "depends_on"]

# Edge types we write.
EDGE_TYPE_CITES = "cites"
EDGE_TYPE_CONTRADICTS = "contradicts"


@register_pass
class EdgesPass(CognifyPass):
    """cognify_edges: auto-infer cites + contradicts edges.

    6-hour cadence. Up to 15 LLM calls per run (contradicts only).
    cites detection is zero-LLM text matching.
    Uses Phase 5 graph_walk for efficient contradiction pair selection.
    """

    pass_name = "edges"

    def run(self, conn: Any, project_id: Any, *, dry_run: bool = False) -> PassResult:
        if project_id is None:
            raise ValueError(
                "cognify_edges requires a project_id (LLM pass; project-scoped)"
            )

        cites_new, contradicts_new, llm_calls = _run_edges(
            conn, project_id, dry_run=dry_run
        )
        return PassResult(
            rows_processed=cites_new + contradicts_new,
            llm_calls=llm_calls,
            metadata={
                "pass": "edges",
                "cites_edges_created": cites_new,
                "contradicts_edges_created": contradicts_new,
            },
        )


def _run_edges(
    conn: Any, project_id: Any, *, dry_run: bool = False
) -> tuple[int, int, int]:
    """Main edges pass logic. Returns (cites_new, contradicts_new, llm_calls)."""
    cites_new = _detect_cites(conn, project_id, dry_run=dry_run)
    contradicts_new, llm_calls = _detect_contradicts(
        conn, project_id, dry_run=dry_run
    )
    return cites_new, contradicts_new, llm_calls


# ── cites detection ───────────────────────────────────────────────────────────


def _detect_cites(
    conn: Any, project_id: Any, *, dry_run: bool = False
) -> int:
    """Detect cites edges via text pattern matching.

    A cites edge is inferred when memory row A's content contains a
    reference pattern that matches memory row B's title (case-insensitive,
    normalised). Deterministic; zero LLM cost.
    """
    # Load all non-archived memory rows for the project.
    memories = _load_memories(conn, project_id)
    if len(memories) < 2:
        return 0

    # Build a title → id index.
    title_index: dict[str, UUID] = {}
    for m in memories:
        if m.get("title"):
            norm = _normalize_title(m["title"])
            if norm:
                title_index[norm] = m["id"]

    new_edges = 0
    for m in memories:
        content = m.get("content") or ""
        for norm_title, target_id in title_index.items():
            if target_id == m["id"]:
                continue
            if re.search(r"\b" + re.escape(norm_title) + r"\b", content, re.IGNORECASE):
                if dry_run:
                    new_edges += 1
                    continue
                inserted = _insert_edge(
                    conn,
                    from_id=m["id"],
                    to_id=target_id,
                    edge_type=EDGE_TYPE_CITES,
                    confidence=0.7,
                    created_by="cognify_edges",
                )
                if inserted:
                    new_edges += 1

    return new_edges


def _normalize_title(title: str) -> str:
    """Lower-case and strip punctuation for fuzzy title matching."""
    return re.sub(r"[^\w\s]", "", title.lower()).strip()


# ── contradicts detection ─────────────────────────────────────────────────────


def _detect_contradicts(
    conn: Any, project_id: Any, *, dry_run: bool = False
) -> tuple[int, int]:
    """Detect contradicts edges using Phase 5 graph_walk + LLM judgment.

    Returns (new_edges, llm_calls).

    Pair selection: for each tier='lesson' or tier='rule' row in the project,
    walk max_hops=2 using derived_from/refined_by/depends_on edges. The
    walker's results are the "nearby" memories — candidate contradiction pairs
    (more likely to be related, so worth comparing). LLM cost ceiling: 15
    calls per pass (MAX_LLM_CALLS_PER_PASS).
    """
    all_memories = _load_memories(conn, project_id)
    if len(all_memories) < 2:
        return 0, 0

    # Seed nodes: only lesson/rule tiers (semantic claims worth contradicting).
    seed_memories = [
        m for m in all_memories if m.get("tier") in CONTRADICTION_SEED_TIERS
    ]
    if not seed_memories:
        # Fall back to all memories if no lesson/rule rows exist yet (e.g. new
        # project that only has 'decision' rows). Cost ceiling still applies.
        seed_memories = all_memories

    candidate_pairs: list[tuple[UUID, UUID]] = []
    seen: set[frozenset] = set()

    for m in seed_memories:
        result = walk(
            conn,
            seed_memory_id=m["id"],
            edge_types=CONTRADICTION_WALK_EDGE_TYPES,
            max_hops=CONTRADICTION_MAX_HOPS,
            max_nodes=CONTRADICTION_MAX_NODES,
            direction="both",
            cross_project=False,
        )
        for neighbor in result.memories:
            if neighbor.id == m["id"]:
                continue
            pair_key = frozenset([m["id"], neighbor.id])
            if pair_key in seen:
                continue
            seen.add(pair_key)
            candidate_pairs.append((m["id"], neighbor.id))

    if not candidate_pairs:
        return 0, 0

    # Build a content index for LLM comparison (all_memories, not just seeds,
    # because neighbors may be from any tier).
    content_by_id: dict[UUID, str] = {
        m["id"]: m.get("content", "") for m in all_memories
    }

    new_edges = 0
    llm_calls = 0
    for from_id, to_id in candidate_pairs[:MAX_LLM_CALLS_PER_PASS]:
        if llm_calls >= MAX_LLM_CALLS_PER_PASS:
            break
        content_a = content_by_id.get(from_id, "")
        content_b = content_by_id.get(to_id, "")
        if not content_a or not content_b:
            continue

        contradicts_flag = _llm_judge_contradiction(content_a, content_b)
        llm_calls += 1

        if contradicts_flag:
            if dry_run:
                new_edges += 1
                continue
            inserted_ab = _insert_edge(
                conn,
                from_id=from_id,
                to_id=to_id,
                edge_type=EDGE_TYPE_CONTRADICTS,
                confidence=0.8,
                created_by="cognify_edges",
            )
            inserted_ba = _insert_edge(
                conn,
                from_id=to_id,
                to_id=from_id,
                edge_type=EDGE_TYPE_CONTRADICTS,
                confidence=0.8,
                created_by="cognify_edges",
            )
            new_edges += inserted_ab + inserted_ba

    return new_edges, llm_calls


def _llm_judge_contradiction(content_a: str, content_b: str) -> bool:
    """Return True if the LLM judges content_a and content_b to contradict.

    Degrades gracefully if the SDK is unavailable or the API key is missing.
    """
    try:
        import anthropic  # noqa: PLC0415
    except ImportError:
        return False

    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return False

    client = anthropic.Anthropic(api_key=api_key)
    prompt = (
        "Do these two memory entries contradict each other? "
        "Reply with only 'YES' or 'NO'.\n\n"
        f"Entry A:\n{content_a[:500]}\n\nEntry B:\n{content_b[:500]}"
    )
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip().upper()
        return text.startswith("YES")
    except Exception as exc:  # noqa: BLE001
        logger.warning("cognify_edges: LLM contradiction check failed: %s", exc)
        return False


# ── Shared helpers ────────────────────────────────────────────────────────────


def _load_memories(conn: Any, project_id: Any) -> list[dict]:
    """Load all non-archived memory rows for a project.

    Returns dicts with keys: id, kind, tier, title, content.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, kind, tier, title, content "
            "FROM devbrain.memory "
            "WHERE project_id = %s AND archived_at IS NULL "
            "ORDER BY created_at",
            (project_id,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _insert_edge(
    conn: Any,
    *,
    from_id: Any,
    to_id: Any,
    edge_type: str,
    confidence: float,
    created_by: str,
) -> int:
    """Insert a memory_dependencies edge. Returns 1 if new, 0 if duplicate."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, confidence, created_by) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            (from_id, to_id, edge_type, confidence, created_by),
        )
        inserted = cur.rowcount
    conn.commit()
    return inserted
