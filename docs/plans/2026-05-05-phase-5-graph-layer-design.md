# Atlas Phase 5 — Graph Layer (memory_edges via recursive CTEs)

> **Status:** Design locked, ready for implementation plan.
>
> **Scope:** Generalize `memory_dependencies` into a typed-edge substrate with
> bounded multi-hop traversal. Improves agent context recall by surfacing
> related memories beyond 1-2 hop neighborhoods.
>
> **Non-goals:** Apache AGE, Cypher syntax, graph algorithms (shortest path,
> PageRank, community detection), edge-property-aware ranking, `worker.py`
> multi-hop refactor. All deferred to Phase 5.x or later.
>
> **Verification gate:** 7 new postulates green + 12-15 walker unit tests +
> all 22 prior postulates still pass.

---

## 1. Drivers

**Primary:** graph-aware `deep_search`. Today's `deep_search` returns text
chunks via pgvector similarity. Adding a `with_graph: bool` switch lets it
also return the graph neighborhood of top-matched memories. Improves
recall quality for "explore related ideas" use cases.

**Secondary:** new `graph_walk` MCP tool. Starts from a known memory_id
(not a text query) and walks the graph. Distinct from `deep_search`
because the agent often has a relevant memory in hand and wants its
neighborhood without a fresh similarity search.

**Pain points addressed:**
- Today's cascade re-eval walks one hop at a time. Multi-hop traversal
  in a single query is awkward in pure SQL.
- The relevance signal in pgvector chunk search washes out when the
  agent's intent is "what's connected to this idea" — graph traversal
  surfaces the connection structure that text similarity misses.

## 2. Locked decisions

| # | Decision |
|---|---|
| 1 | Primary driver: graph-aware `deep_search`; secondary: standalone `graph_walk` |
| 2 | Backend: plain recursive CTEs on `memory_dependencies` — no Apache AGE |
| 3 | 6 edge types: `cites`, `depends_on`, `supersedes`, `contradicts`, `derived_from`, `refined_by` |
| 4 | Population: manual via `store()` params + curator backfill (Phase 6 cognify) |
| 5 | Schema: relax `memory_dependencies` CHECK constraint; no rename |
| 6 | Project scoping: default same-project; `cross_project=True` matches `deep_search` semantics |
| 7 | `graph_walk` defaults: `max_hops=3`, `max_nodes=50`, `edge_types=strong-signal subset`, `direction='both'` |
| 8 | Response shape: `{memories, edges, truncated}` — BFS edge order; memories sorted (hops asc, strength desc) |
| 9 | `deep_search` adds `with_graph: bool = false`; top-level `graph` field when set |
| 10 | `store()` adds `derived_from` + `refined_by` params alongside existing `depends_on` + `supersedes` |

### Why CTEs and not Apache AGE

| | Apache AGE | Recursive CTEs |
|---|---|---|
| Query expressiveness | Cypher: natural for graph traversal | Verbose; fine for bounded-hop walks |
| Install cost | Postgres extension; sudo on every devbrain-db host | Zero — stock Postgres |
| Operational complexity | `ag_catalog` schema, vlabel/elabel tables, Cypher-in-SQL string concat | Single table, plain SQL |
| Killer features | shortest path, PageRank, community detection | None of those without writing them |
| Use-case fit (1-3 hop bounded exploration) | Overkill | Cleanly fits |

The driver — agent finds related ideas — caps at 3-4 hops in practice
(relevance signal washes out further). The killer features AGE provides
aren't on the roadmap. Stock Postgres handles our shape (sparse graph,
~10k memory rows, low branching factor) in <100ms for 3-hop traversals.

If a real Cypher-shaped query shows up later (shortest path between two
memories, weight-decay traversal, graph centrality), AGE can be added on
top of the same `memory_dependencies` table. Schema doesn't preclude it.

## 3. Schema migration (`migrations/024_memory_edges_generalize.sql`)

```sql
-- ═══════════════════════════════════════════════════════════════════
-- 024: Generalize memory_dependencies for Phase 5 graph layer
-- ═══════════════════════════════════════════════════════════════════
--
-- Phase 5 (graph layer) extends memory_dependencies from 4 edge types
-- to 6 by relaxing the CHECK constraint. No table rename, no column
-- additions — the existing shape is already the general edge model.
--
--   - 'derived_from'  → from extracted from to (lessons from sessions)
--   - 'refined_by'    → to is a sharpening/elaboration of from

ALTER TABLE devbrain.memory_dependencies
    DROP CONSTRAINT IF EXISTS memory_dependencies_edge_type_check;

ALTER TABLE devbrain.memory_dependencies
    ADD CONSTRAINT memory_dependencies_edge_type_check
    CHECK (edge_type IN (
        'cites', 'depends_on', 'supersedes', 'contradicts',
        'derived_from', 'refined_by'
    ));
```

**Edge metadata convention (docstring guidance, no schema change):**
- `derived_from`: `metadata = {"source_session_id": "...", "extraction_confidence": 0.85}`
- `refined_by`: `metadata = {"refinement_note": "...", "evidence_memory_ids": [...]}`
- `store()` MAY pass an optional `metadata: dict` per edge — implementer's choice for v1.

**Test:** `factory/tests/test_migration_024.py` asserts the CHECK constraint
accepts all 6 types, rejects unknowns, preserves existing rows.

## 4. Walker module (`factory/graph/walker.py`)

**Public API:**

```python
@dataclass
class GraphWalkResult:
    memories: list[MemoryRef]
    edges: list[EdgeRef]
    truncated: bool

def walk(
    conn,
    seed_memory_id: UUID,
    *,
    edge_types: list[str] | None = None,
    max_hops: int = 3,
    max_nodes: int = 50,
    direction: Literal["outgoing", "incoming", "both"] = "both",
    cross_project: bool = False,
) -> GraphWalkResult: ...
```

**Recursive CTE shape (paraphrased):**

```sql
WITH RECURSIVE walk AS (
    SELECT id, project_id, ARRAY[id]::uuid[] AS visited, 0 AS hops
    FROM devbrain.memory WHERE id = $seed AND archived_at IS NULL

    UNION ALL

    SELECT next.id, next.project_id, w.visited || next.id, w.hops + 1
    FROM walk w
    JOIN devbrain.memory_dependencies d
      ON (($direction='outgoing' OR $direction='both') AND d.from_memory_id = w.id)
      OR (($direction='incoming' OR $direction='both') AND d.to_memory_id = w.id)
    JOIN devbrain.memory next
      ON next.id = CASE WHEN d.from_memory_id = w.id
                        THEN d.to_memory_id ELSE d.from_memory_id END
    WHERE w.hops < $max_hops
      AND NOT next.id = ANY(w.visited)
      AND next.archived_at IS NULL
      AND ($edge_types IS NULL OR d.edge_type = ANY($edge_types))
      AND ($cross_project OR next.project_id = (SELECT project_id FROM walk WHERE hops = 0))
)
SELECT DISTINCT ON (id) id, hops FROM walk
ORDER BY id, hops ASC
LIMIT $max_nodes;
```

**Cycle handling:** `visited` array accumulator + `NOT id = ANY(visited)`.
Cheap because Postgres array membership is fast for small arrays (we cap
at 50 nodes). Larger walks would need a separate visited table — out of
scope.

**Truncation signal:** if `LIMIT max_nodes` was hit, `truncated=True` so
caller knows there's more graph available. Caller can re-invoke with
higher `max_nodes` or narrower `edge_types`.

**Edges fetched in a separate query** after the memory walk completes —
keeps the recursive CTE clean. The edges query joins
`memory_dependencies` filtered by `from_id IN visited AND to_id IN visited`.

## 5. MCP tool surface

### 5.1 New tool: `graph_walk`

```typescript
{
  name: "graph_walk",
  description: "Explore the memory graph from a known memory. Returns related memories within max_hops, filtered by edge types. Use when you have a relevant memory_id (from deep_search or store result) and want to see its neighborhood.",
  parameters: {
    seed_memory_id: { type: "string", required: true },
    edge_types:     { type: "array", items: "string",
                      default: ["supersedes","refined_by","derived_from","depends_on"] },
    max_hops:       { type: "number", default: 3, minimum: 1, maximum: 6 },
    max_nodes:      { type: "number", default: 50, minimum: 5, maximum: 200 },
    direction:      { type: "string", enum: ["outgoing","incoming","both"], default: "both" },
    cross_project:  { type: "boolean", default: false },
  }
}
```

Returns `{memories, edges, truncated}` straight from `walker.walk()`.

### 5.2 `deep_search` extension

New optional params:
- `with_graph: bool = false` — runs walker on top N chunks' parent memories.
- `graph_max_hops: number = 3` (only when `with_graph=true`)
- `graph_max_nodes: number = 50` (only when `with_graph=true`)
- Edge types and direction inherit walker defaults; not exposed on `deep_search`.

Response gains a top-level `graph` field when `with_graph=true`:
```typescript
{
  results: [...],            // existing chunks, unchanged
  graph?: {
    memories: [...],         // deduped union from walker
    edges: [...],            // BFS order
    seeds: [memory_id, ...], // walker seeds (top chunks' parent memories)
  }
}
```

### 5.3 `store()` extension

Adds two params mirroring existing `depends_on`/`supersedes`:
- `derived_from: list[UUID]`
- `refined_by: list[UUID]`

Same idempotency semantics as existing edge params (ON CONFLICT DO NOTHING
via the triplet unique).

## 6. Postulates + verification

**New postulates** (under `tests/postulates/`):

| Postulate | Asserts |
|---|---|
| `P_graph_bounded_hops` | A graph with edges to depth 5 returns at most `max_hops=3` levels |
| `P_graph_bounded_nodes` | A graph with 100 reachable memories returns at most `max_nodes=50` and sets `truncated=True` |
| `P_graph_cycle_safe` | A cyclic graph A→B→C→A terminates correctly |
| `P_graph_same_project` | Default `cross_project=False` doesn't surface other-project memories |
| `P_graph_cross_project` | `cross_project=True` surfaces other-project memories |
| `P_graph_edge_filter` | `edge_types=['supersedes']` only walks supersedes edges |
| `P_graph_archived_excluded` | Archived memories don't appear; walker stops at archived nodes |

**Existing tests must still pass:** all 22 prior postulates (especially P3 — same-project isolation), 5 curator-brief tests, 5 per-rule postulates, `devbrain rules lint`.

**Integration tests:**
- `factory/tests/test_graph_walker.py` — 12-15 walker unit tests
- `factory/tests/test_store_graph_edges.py` — `store()` with `derived_from`/`refined_by`
- `factory/tests/test_deep_search_with_graph.py` — `with_graph=true/false`

**Coverage gate:** `factory/graph/walker.py` ≥ 90%.

## 7. Sub-PR sequence

```
5a (migration + walker)  →  5b (graph_walk MCP tool)
                         →  5c (store() edge params)
                         →  5d (deep_search with_graph — Phase 5 closeout)
```

Sequential merge on CI green.

| Sub-PR | Title | Scope |
|---|---|---|
| 5a | `feat(graph): Atlas Phase 5a — memory_edges generalization + graph walker` | Migration 024, `factory/graph/walker.py`, 7 postulates, 12-15 walker tests |
| 5b | `feat(graph): Atlas Phase 5b — graph_walk MCP tool` | TS tool registration + dist rebuild + manual smoke |
| 5c | `feat(graph): Atlas Phase 5c — store() supports derived_from + refined_by edges` | TS store extension + Python tests |
| 5d | `feat(graph): Atlas Phase 5d — deep_search graph-aware mode (Phase 5 done)` | TS deep_search extension + Python integration tests + DevBrain milestone store |

Each PR independently reviewable. Merge gates: CI green + postulates green + manual smoke for the MCP-touching PRs (5b, 5d).

## 8. Out of scope (explicitly deferred)

- **Apache AGE** — Phase 5.x or later, only if Cypher-shaped queries appear
- **Auto-inferred `cites` from text** — Phase 6 cognify
- **Auto-inferred `contradicts` detection** — Phase 6 cognify or eval agent
- **Edge-level `memory_ledger` audit rows** — defer until regulator pressure
- **`worker.py` multi-hop refactor** — Phase 5.x perf optimization, not v1 correctness
- **Edge property metadata schema** — informal docstring guidance for v1 (`metadata: jsonb` already exists; conventions only)
- **`graph_walk` rate limiting / per-tenant query budget** — defer; trust the team for now
- **Postgres performance tuning** (custom indexes on `memory_dependencies` for graph traversal patterns) — measure first; the existing indexes (`idx_memory_dep_from`, `idx_memory_dep_to`, `idx_memory_dep_type`) cover the recursive CTE access patterns

## 9. Forward compatibility with Phase 6

Phase 5's design is intentionally narrow so it doesn't foreclose Phase 6
options:

- **`memory_dependencies` shape stays general.** Phase 6 cognify will populate
  more edge types automatically (`cites`, `contradicts`, additional
  `derived_from` from session re-extraction). Same table, same schema.
- **Walker is project-scoped by default.** When Phase 6 cognify runs offline
  background passes, it can call `walker.walk(..., cross_project=False)` for
  per-project work and `cross_project=True` for cross-project cognify
  ("which canonical rules has any project violated lately?").
- **Edge metadata field is open.** Phase 6's automated populator will write
  whatever telemetry it wants — extraction confidence, source session,
  cognify pass timestamp — without schema migration.

## 10. References

- `docs/plans/2026-05-02-session-continuation-playbook.md` §9 — original Phase 5 sketch
- DevBrain decision `00b295f0` — canonical 'devbrain' project as cross-project rules library (PR #91)
- DevBrain decision `9287ab95` — Atlas Phase 3 complete milestone
- Migration 014 — `memory_dependencies` original schema
- `factory/curator/worker.py` — current 1-hop iterative cascade drainer (untouched in Phase 5)
