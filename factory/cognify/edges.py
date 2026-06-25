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

Spend tracking: each LLM call for contradiction detection writes a row to
devbrain.cognify_spend_log via observability.spend.record_spend.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import Any
from uuid import UUID

from cognify.orchestrator import CognifyPass, PassResult, register_pass
from graph.walker import walk
from observability.pricing import SONNET_4_6, compute_cost_usd
from observability.spend import record_spend

logger = logging.getLogger(__name__)

# Max LLM calls per pass for contradicts detection.
MAX_LLM_CALLS_PER_PASS = 15

# Model used for contradiction judgment. Must match a key in observability/pricing.py.
_EDGES_MODEL = "claude-sonnet-4-6"

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

    cross_project (default False): when True, the contradiction walker
    surfaces canonical 'devbrain' rules-library memories alongside the
    project's own memories as candidate pairs. Use to detect when a
    project's lesson/rule contradicts a regulatory rule in the canonical
    library. When False, traversal is strict same-project (P3-aligned).
    """

    pass_name = "edges"

    def run(
        self,
        conn: Any,
        project_id: Any,
        *,
        dry_run: bool = False,
        cross_project: bool = False,
        max_llm_calls: int | None = None,
    ) -> PassResult:
        if project_id is None:
            raise ValueError(
                "cognify_edges requires a project_id (LLM pass; project-scoped)"
            )

        cites_new, contradicts_new, llm_calls = _run_edges(
            conn, project_id,
            dry_run=dry_run,
            cross_project=cross_project,
            max_llm_calls=max_llm_calls,
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
    conn: Any,
    project_id: Any,
    *,
    dry_run: bool = False,
    cross_project: bool = False,
    max_llm_calls: int | None = None,
) -> tuple[int, int, int]:
    """Main edges pass logic. Returns (cites_new, contradicts_new, llm_calls).

    cites detection stays strictly project-local (text-pattern match;
    no walker involvement). Only contradicts detection honors
    cross_project when True.

    max_llm_calls: per-pass ceiling on contradicts-detection LLM calls.
    When None (default), uses MAX_LLM_CALLS_PER_PASS=15. cites detection
    is zero-LLM and ignores this.
    """
    # Load the project's non-archived memory once and share it with both
    # detectors — within a single pass the snapshot is identical, so the
    # previous two separate full-table reads were pure duplication.
    memories = _load_memories(conn, project_id)
    cites_new = _detect_cites(
        conn, project_id, dry_run=dry_run, memories=memories
    )
    contradicts_new, llm_calls = _detect_contradicts(
        conn, project_id, dry_run=dry_run, record_conn=conn,
        cross_project=cross_project,
        max_llm_calls=max_llm_calls,
        memories=memories,
    )
    return cites_new, contradicts_new, llm_calls


# ── cites detection ───────────────────────────────────────────────────────────


def _detect_cites(
    conn: Any, project_id: Any, *, dry_run: bool = False, memories: list | None = None
) -> int:
    """Detect cites edges via text pattern matching.

    A cites edge is inferred when memory row A's content contains a
    reference pattern that matches memory row B's title (case-insensitive,
    normalised). Deterministic; zero LLM cost.

    `memories` lets the caller pass a pre-loaded snapshot so the edges
    pass reads the table only once; when None it loads its own (keeps the
    direct-call test contract).
    """
    if memories is None:
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
    if not title_index:
        return 0

    # Index titles by their FIRST word, and pre-compile each title's
    # word-boundary pattern ONCE.
    #
    # Exactness: a `\bTITLE\b` match is impossible unless TITLE's first word
    # appears as a whole word in the content — the leading `\b` plus the
    # non-word char that follows the first word *inside* TITLE force that
    # word to be a maximal `\w`-run in the content. So we only test titles
    # whose first word is present in the content's word set, turning the old
    # O(rows × all-titles) scan into O(rows × content-words × small-bucket)
    # WITHOUT changing which edges are produced (the regex still confirms the
    # full boundary match). This replaces the per-(row,title) substring scan
    # that, while cheap per op, still ran rows×titles times.
    titles_by_first_word: dict[str, list[tuple[str, UUID]]] = defaultdict(list)
    compiled: dict[str, "re.Pattern[str]"] = {}
    for norm_title, target_id in title_index.items():
        first = re.match(r"\w+", norm_title)
        if first is None:
            continue  # no leading word char → can never \b-match
        titles_by_first_word[first.group()].append((norm_title, target_id))
        compiled[norm_title] = re.compile(
            r"\b" + re.escape(norm_title) + r"\b", re.IGNORECASE
        )

    new_edges = 0
    for m in memories:
        content = m.get("content") or ""
        if not content:
            continue
        content_lower = content.lower()
        m_id = m["id"]
        # Only titles whose first word is a whole word in this content can
        # match. Each title lives in exactly one first-word bucket, and each
        # distinct content word is visited once, so every (row, target) pair
        # is considered at most once — same as iterating all titles, fewer
        # candidates. The substring `in` then the regex confirm the rest.
        for word in set(re.findall(r"\w+", content_lower)):
            for norm_title, target_id in titles_by_first_word.get(word, ()):
                if target_id == m_id:
                    continue
                if norm_title not in content_lower:
                    continue
                if compiled[norm_title].search(content):
                    if dry_run:
                        new_edges += 1
                        continue
                    inserted = _insert_edge(
                        conn,
                        from_id=m_id,
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
    conn: Any,
    project_id: Any,
    *,
    dry_run: bool = False,
    record_conn: Any = None,
    cross_project: bool = False,
    max_llm_calls: int | None = None,
    memories: list | None = None,
) -> tuple[int, int]:
    """Detect contradicts edges using Phase 5 graph_walk + LLM judgment.

    Returns (new_edges, llm_calls).

    Pair selection: for each tier='lesson' or tier='rule' row in the project,
    walk max_hops=2 using derived_from/refined_by/depends_on edges. The
    walker's results are the "nearby" memories — candidate contradiction pairs
    (more likely to be related, so worth comparing). LLM cost ceiling: 15
    calls per pass (MAX_LLM_CALLS_PER_PASS).

    record_conn: connection used to write spend log rows. When None, spend
        is not recorded (e.g. dry_run or test scenarios without the table).
        In production, pass the same conn used for edge writes.

    cross_project: when True, the walker surfaces canonical 'devbrain'
        rules-library memories (tier='rule' with non-empty
        compliance_profiles) as candidate pairs alongside the project's
        own memories. Detects when a project's lesson contradicts a
        canonical regulatory rule. When False (default), traversal stays
        strict same-project (P3-aligned).
    """
    all_memories = (
        memories if memories is not None else _load_memories(conn, project_id)
    )
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

    # Only candidate_pairs[:cap] is ever LLM-judged, so stop walking seeds
    # once we have `cap` pairs — the walks (4 DB round-trips each) are the
    # expensive part, and pairs are appended in deterministic seed order so
    # the first `cap` are identical either way.
    cap = max_llm_calls if max_llm_calls is not None else MAX_LLM_CALLS_PER_PASS
    candidate_pairs: list[tuple[UUID, UUID]] = []
    seen: set[frozenset] = set()

    for m in seed_memories:
        if len(candidate_pairs) >= cap:
            break
        result = walk(
            conn,
            seed_memory_id=m["id"],
            edge_types=CONTRADICTION_WALK_EDGE_TYPES,
            max_hops=CONTRADICTION_MAX_HOPS,
            max_nodes=CONTRADICTION_MAX_NODES,
            direction="both",
            cross_project=cross_project,
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
    for from_id, to_id in candidate_pairs[:cap]:
        if llm_calls >= cap:
            break
        content_a = content_by_id.get(from_id, "")
        content_b = content_by_id.get(to_id, "")
        if not content_a or not content_b:
            continue

        contradicts_flag, usage = _llm_judge_contradiction(content_a, content_b)
        llm_calls += 1

        # Record spend for this LLM call (non-zero tokens only — skip mock/no-API runs).
        if record_conn is not None and any(
            usage.get(k, 0) for k in ("input_tokens", "output_tokens")
        ):
            cost = compute_cost_usd(
                SONNET_4_6,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_read_tokens=usage.get("cache_read_tokens", 0),
                cache_write_tokens=usage.get("cache_write_tokens", 0),
            )
            record_spend(
                record_conn,
                project_id=project_id,
                pass_name="edges",
                model=_EDGES_MODEL,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cache_read_tokens=usage.get("cache_read_tokens", 0),
                cache_write_tokens=usage.get("cache_write_tokens", 0),
                cost_usd=cost,
            )

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


def _llm_judge_contradiction(
    content_a: str, content_b: str
) -> tuple[bool, dict]:
    """Return (contradicts, usage) — whether the LLM judges content_a and
    content_b to contradict, plus token usage for spend tracking.

    usage dict keys: input_tokens, output_tokens, cache_read_tokens,
    cache_write_tokens. All are 0 when the LLM was not called.

    Degrades gracefully if the SDK is unavailable or the API key is missing.
    """
    _empty_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    try:
        import anthropic  # noqa: PLC0415
    except ImportError:
        return False, _empty_usage

    from cognify._anthropic_auth import (
        claude_code_system_prefix,
        resolve_anthropic_auth,
    )
    auth_kwargs = resolve_anthropic_auth()
    if auth_kwargs is None:
        return False, _empty_usage

    client = anthropic.Anthropic(**auth_kwargs)
    prompt = (
        "Do these two memory entries contradict each other? "
        "Reply with only 'YES' or 'NO'.\n\n"
        f"Entry A:\n{content_a[:500]}\n\nEntry B:\n{content_b[:500]}"
    )
    # OAuth path requires the Claude Code SDK fingerprint as the system
    # prompt — without it /v1/messages 429s on subscription tokens.
    # Console API key path returns None and the call uses no system prompt.
    create_kwargs: dict[str, Any] = {
        "model": _EDGES_MODEL,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": prompt}],
    }
    oauth_prefix = claude_code_system_prefix()
    if oauth_prefix:
        create_kwargs["system"] = oauth_prefix
    try:
        response = client.messages.create(**create_kwargs)
        usage = response.usage
        token_usage = {
            "input_tokens": getattr(usage, "input_tokens", 0),
            "output_tokens": getattr(usage, "output_tokens", 0),
            "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
            "cache_write_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        }
        text = response.content[0].text.strip().upper()
        return text.startswith("YES"), token_usage
    except Exception as exc:  # noqa: BLE001
        logger.warning("cognify_edges: LLM contradiction check failed: %s", exc)
        return False, _empty_usage


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
