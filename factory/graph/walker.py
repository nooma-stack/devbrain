"""factory.graph.walker — bounded recursive-CTE graph walker.

Traverses devbrain.memory_dependencies using plain Postgres recursive
CTEs. No Apache AGE, no Cypher — stock SQL that runs on any Postgres 13+
instance (DevBrain uses 17).

Public API
----------
    result = walk(
        conn,
        seed_memory_id,
        edge_types=["supersedes", "derived_from"],
        max_hops=3,
        max_nodes=50,
        direction="both",
        cross_project=False,
    )
    # result.memories  → list[MemoryRef]   (BFS order, hops asc, strength desc)
    # result.edges     → list[EdgeRef]     (edges within returned node set)
    # result.truncated → bool              (True if max_nodes was hit)

Design notes
------------
- Cycle safety: a `visited` UUID[] accumulator in the recursive CTE
  prevents revisiting nodes. Postgres array membership (`= ANY(array)`)
  is fast for small arrays; we cap at max_nodes=200 so there's no
  out-of-memory risk.
- Truncation: the walker fetches max_nodes + 1 seed rows from the CTE.
  If exactly max_nodes+1 rows come back, the caller gets max_nodes rows
  and truncated=True. The extra row is discarded, not counted.
- Direction "both": in the CTE we look at BOTH directions each step.
  `from_memory_id = w.id` expands outgoing edges; `to_memory_id = w.id`
  expands incoming. The "next" node is whichever end of the edge isn't
  the current node.
- Project scoping: by default the walker stays within the seed memory's
  project. cross_project=True lifts that restriction. The seed node's
  project_id is captured in the base case (hops=0) and carried forward.
  For cross_project=False we compare `next.project_id = seed.project_id`
  at each expansion step.
- Archived exclusion: both the seed and all expanded nodes are filtered
  by `archived_at IS NULL`. The walker stops at archived boundaries —
  it won't traverse through an archived node even if edges exist beyond
  it (because archived nodes are excluded from the RECURSIVE part too).
- Edges returned: after the recursive walk, a second query fetches all
  edges whose BOTH endpoints are in the visited set. This keeps the CTE
  clean and avoids tracking edge data in the recursive accumulator.

See §4 of docs/plans/2026-05-05-phase-5-graph-layer-design.md for the
full design rationale.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

# All 6 Phase-5 edge types. The "strong signal" default subset is the 4
# that carry semantic weight; 'cites' is weaker (narrative reference) and
# 'contradicts' surfaces integrity issues rather than knowledge paths.
ALL_EDGE_TYPES: list[str] = [
    "cites",
    "depends_on",
    "supersedes",
    "contradicts",
    "derived_from",
    "refined_by",
]

STRONG_SIGNAL_EDGE_TYPES: list[str] = [
    "supersedes",
    "refined_by",
    "derived_from",
    "depends_on",
]


@dataclass
class MemoryRef:
    """A memory row as returned by the walker."""

    id: uuid.UUID
    project_id: uuid.UUID
    kind: str
    title: str | None
    content: str
    strength: float
    hops: int  # BFS distance from seed (0 = seed itself)


@dataclass
class EdgeRef:
    """A memory_dependencies edge within the returned node set."""

    from_memory_id: uuid.UUID
    to_memory_id: uuid.UUID
    edge_type: str
    confidence: float


@dataclass
class GraphWalkResult:
    """Result of a walker.walk() call."""

    memories: list[MemoryRef] = field(default_factory=list)
    edges: list[EdgeRef] = field(default_factory=list)
    truncated: bool = False


def walk(
    conn,
    seed_memory_id: uuid.UUID | str,
    *,
    edge_types: list[str] | None = None,
    max_hops: int = 3,
    max_nodes: int = 50,
    direction: Literal["outgoing", "incoming", "both"] = "both",
    cross_project: bool = False,
) -> GraphWalkResult:
    """Traverse the memory graph from a seed memory using a recursive CTE.

    Parameters
    ----------
    conn:
        A psycopg2 connection. The caller owns the connection lifecycle;
        walk() never commits or rolls back.
    seed_memory_id:
        UUID of the starting memory row. Must exist and be non-archived;
        returns an empty result if it doesn't.
    edge_types:
        Edge type filter. None / empty → strong-signal defaults
        (supersedes, refined_by, derived_from, depends_on). Pass an
        explicit list to restrict further or to include 'cites' /
        'contradicts'.
    max_hops:
        Maximum BFS depth. Capped at 6. Default 3.
    max_nodes:
        Maximum total nodes (including seed) to return. Capped at 200.
        If more nodes exist, truncated=True is set. Default 50.
    direction:
        'outgoing' — follow from_memory_id→to_memory_id edges only.
        'incoming' — follow reverse edges (who points at me?).
        'both'     — expand in both directions each hop. Default.
    cross_project:
        If False (default), expansion stays within the seed's project.
        If True, the walker crosses project boundaries (use only when
        the caller has explicit cross-project intent).

    Returns
    -------
    GraphWalkResult with memories sorted by (hops asc, strength desc),
    edges within the returned node set, and a truncated flag.
    """
    seed_id = str(seed_memory_id)

    # Apply caps from design doc
    max_hops = min(max_hops, 6)
    max_nodes = min(max_nodes, 200)

    # Resolve effective edge types
    effective_edge_types = edge_types if edge_types else STRONG_SIGNAL_EDGE_TYPES

    with conn.cursor() as cur:
        # ── Recursive CTE walk ────────────────────────────────────────────────
        #
        # Base case: the seed memory (hops=0), filtered by archived_at.
        # Recursive case: expand via memory_dependencies, tracking visited
        # to prevent cycles, stopping at max_hops.
        #
        # The LIMIT is max_nodes + 1 so we can detect truncation.
        #
        # Direction handling: we use a CASE expression to pick the "next"
        # node — the endpoint of the edge that isn't the current node.
        #
        # Cross-project handling: we capture the seed's project_id in the
        # base case. In the recursive case we filter next.project_id when
        # cross_project=False. Because the seed project_id propagates
        # through `w.seed_project_id`, every expansion step knows the
        # original project without a subquery.

        # Build the direction filter clauses
        if direction == "outgoing":
            dir_join = "d.from_memory_id = w.id"
            next_id_expr = "d.to_memory_id"
        elif direction == "incoming":
            dir_join = "d.to_memory_id = w.id"
            next_id_expr = "d.from_memory_id"
        else:  # both
            dir_join = "(d.from_memory_id = w.id OR d.to_memory_id = w.id)"
            next_id_expr = (
                "CASE WHEN d.from_memory_id = w.id "
                "THEN d.to_memory_id ELSE d.from_memory_id END"
            )

        # Parameterized query — no string interpolation for user data
        sql = f"""
WITH RECURSIVE walk AS (
    -- Base case: the seed memory
    SELECT
        m.id                         AS id,
        m.project_id                 AS project_id,
        m.project_id                 AS seed_project_id,
        ARRAY[m.id::uuid]            AS visited,
        0                            AS hops
    FROM devbrain.memory m
    WHERE m.id = %(seed_id)s::uuid
      AND m.archived_at IS NULL

    UNION ALL

    -- Recursive case: expand one hop
    SELECT
        next_m.id                    AS id,
        next_m.project_id            AS project_id,
        w.seed_project_id            AS seed_project_id,
        w.visited || next_m.id::uuid AS visited,
        w.hops + 1                   AS hops
    FROM walk w
    JOIN devbrain.memory_dependencies d
      ON {dir_join}
    JOIN devbrain.memory next_m
      ON next_m.id = ({next_id_expr})
    WHERE w.hops < %(max_hops)s
      AND NOT (next_m.id = ANY(w.visited))
      AND next_m.archived_at IS NULL
      AND d.edge_type = ANY(%(edge_types)s::text[])
      AND (%(cross_project)s OR next_m.project_id = w.seed_project_id)
),
-- Deduplicate: each node at its minimum hop distance
deduped AS (
    SELECT DISTINCT ON (id) id, project_id, hops
    FROM walk
    ORDER BY id, hops ASC
)
SELECT id, project_id, hops
FROM deduped
ORDER BY hops ASC
LIMIT %(fetch_limit)s
"""
        cur.execute(
            sql,
            {
                "seed_id": seed_id,
                "max_hops": max_hops,
                "edge_types": effective_edge_types,
                "cross_project": cross_project,
                "fetch_limit": max_nodes + 1,  # +1 for truncation detection
            },
        )
        raw_nodes = cur.fetchall()

    if not raw_nodes:
        return GraphWalkResult()

    # Detect truncation
    truncated = len(raw_nodes) > max_nodes
    node_rows = raw_nodes[:max_nodes]

    visited_ids = [str(row[0]) for row in node_rows]
    hops_by_id = {str(row[0]): row[2] for row in node_rows}

    # ── Fetch full memory details for visited nodes ───────────────────────────
    with conn.cursor() as cur:
        cur.execute(
            """
SELECT id, project_id, kind, title, content, strength
FROM devbrain.memory
WHERE id = ANY(%(ids)s::uuid[])
  AND archived_at IS NULL
""",
            {"ids": visited_ids},
        )
        mem_rows = cur.fetchall()

    memories: list[MemoryRef] = []
    for row in mem_rows:
        mem_id = str(row[0])
        memories.append(
            MemoryRef(
                id=row[0],
                project_id=row[1],
                kind=row[2],
                title=row[3],
                content=row[4],
                strength=float(row[5]) if row[5] is not None else 1.0,
                hops=hops_by_id.get(mem_id, 0),
            )
        )

    # Sort by (hops asc, strength desc) per design doc §8
    memories.sort(key=lambda m: (m.hops, -m.strength))

    # ── Fetch edges within the visited node set ───────────────────────────────
    with conn.cursor() as cur:
        cur.execute(
            """
SELECT from_memory_id, to_memory_id, edge_type, confidence
FROM devbrain.memory_dependencies
WHERE from_memory_id = ANY(%(ids)s::uuid[])
  AND to_memory_id   = ANY(%(ids)s::uuid[])
  AND edge_type = ANY(%(edge_types)s::text[])
ORDER BY from_memory_id, to_memory_id
""",
            {"ids": visited_ids, "edge_types": effective_edge_types},
        )
        edge_rows = cur.fetchall()

    edges: list[EdgeRef] = [
        EdgeRef(
            from_memory_id=row[0],
            to_memory_id=row[1],
            edge_type=row[2],
            confidence=float(row[3]) if row[3] is not None else 1.0,
        )
        for row in edge_rows
    ]

    return GraphWalkResult(memories=memories, edges=edges, truncated=truncated)
