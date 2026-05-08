// Recency-neighbor expansion + supersedes auto-walk for deep_search.
//
// When deep_search returns a memory chunk on an active topic, callers
// (Claude sessions in particular) tend to reason from it as if it's
// current state. In fact the chunk may be:
//
//   1. SUPERSEDED — there's a newer memory linked via a
//      `supersedes` edge in devbrain.memory_dependencies. Writers
//      don't always populate the edge, so the older chunk still
//      ranks high on its own merits.
//
//   2. STALE — more recent activity on the same topic exists with no
//      formal supersedes edge, only embedding similarity.
//
// This module surfaces both signals as additive annotations on the
// existing deep_search response — the consumer can detect supersession
// without requiring writers to manually populate dependency edges.
//
// Concrete trigger (2026-05-08): a Claude session surfaced a 04-30
// morning Vertex-quota denial issue as a current Phase-3 blocker, when
// the same evening's session summary recorded the quota approval +
// production flip via PR #201. The supersedes edge wasn't set; the
// issue chunk ranked higher on the search query than the resolution.
//
// All queries are batched (single round-trip per primary set) so that
// deep_search retains its sub-second latency budget on a 50K-row
// project.

import type { QueryResult } from 'pg'
import { query } from './db.js'

/** A primary deep_search result shape we pull annotations for. */
export interface PrimaryRef {
  /** UUID of the devbrain.memory row. */
  memory_id: string
}

/** Knobs for the recency-neighbor expansion. */
export interface RecencyExpandOptions {
  /** Top-K neighbors per primary to return (default 2, hard cap 10). */
  k: number
  /**
   * Don't surface a chunk as a "recency neighbor" unless it's at least
   * this many days newer than the primary. Default 3. The 3-day floor
   * filters out near-duplicates from the same chunking pass.
   */
  minAgeDays: number
  /**
   * Cosine-similarity floor for neighbors. Below this, the chunk is
   * topically unrelated. Default 0.4. Note: deep_search's primary
   * candidates often score ≥0.5; 0.4 admits adjacent context without
   * pulling in noise.
   */
  similarityFloor: number
}

export const DEFAULT_RECENCY_OPTIONS: RecencyExpandOptions = {
  k: 2,
  minAgeDays: 3,
  similarityFloor: 0.4,
}

export interface RecencyNeighbor {
  memory_id: string
  kind: string
  snippet: string
  created_at: string
  similarity: number
  age_delta_days: number
}

/**
 * For each primary memory_id, return the top-K most-recent memory rows
 * (within the same project, non-archived) whose embedding similarity
 * exceeds the floor AND that are at least `minAgeDays` newer.
 *
 * Returns a Map keyed by primary memory_id. Primaries with no
 * qualifying neighbors are absent from the map.
 *
 * Uses ONE batched LATERAL query — the inner subquery runs once per
 * primary, but Postgres reuses the prepared plan and the IVF embedding
 * index, keeping us inside the deep_search latency budget.
 */
export async function expandRecencyNeighbors(
  primaryMemoryIds: string[],
  opts: RecencyExpandOptions = DEFAULT_RECENCY_OPTIONS,
): Promise<Map<string, RecencyNeighbor[]>> {
  const result = new Map<string, RecencyNeighbor[]>()
  if (primaryMemoryIds.length === 0) return result

  const k = Math.max(1, Math.min(opts.k, 10))
  // Overfetch from the IVF index so the post-filter on created_at +
  // similarity floor still has enough candidates after the cuts.
  // 5x is empirical — generous enough that even a stale primary in a
  // dense topic still finds K neighbors, tight enough that the LATERAL
  // doesn't blow up.
  const overfetch = Math.max(20, k * 5)

  const sql = `
WITH primaries AS (
    SELECT id, created_at, embedding, project_id
      FROM devbrain.memory
     WHERE id = ANY($1::uuid[])
       AND archived_at IS NULL
       AND embedding IS NOT NULL
)
SELECT
    p.id::text AS primary_id,
    n.id::text AS neighbor_id,
    n.kind     AS neighbor_kind,
    n.content  AS neighbor_content,
    n.created_at AS neighbor_created_at,
    1 - (n.embedding <=> p.embedding) AS similarity,
    EXTRACT(EPOCH FROM (n.created_at - p.created_at)) / 86400.0
        AS age_delta_days
  FROM primaries p
  CROSS JOIN LATERAL (
        SELECT m.id, m.kind, m.content, m.created_at, m.embedding
          FROM devbrain.memory m
         WHERE m.id <> p.id
           AND m.archived_at IS NULL
           AND m.embedding IS NOT NULL
           AND m.project_id = p.project_id
           AND m.created_at > p.created_at + ($2 || ' days')::interval
         ORDER BY m.embedding <=> p.embedding
         LIMIT $3
       ) n
 WHERE 1 - (n.embedding <=> p.embedding) >= $4
 ORDER BY p.id, n.created_at DESC
`
  const rows: QueryResult = await query(sql, [
    primaryMemoryIds,
    String(opts.minAgeDays),
    overfetch,
    opts.similarityFloor,
  ])

  for (const row of rows.rows) {
    const primaryId = String(row.primary_id)
    const neighbors = result.get(primaryId) ?? []
    if (neighbors.length >= k) continue
    const content = String(row.neighbor_content)
    neighbors.push({
      memory_id: String(row.neighbor_id),
      kind: String(row.neighbor_kind),
      snippet: content.length > 240 ? content.slice(0, 240) + '…' : content,
      created_at: new Date(row.neighbor_created_at as string).toISOString(),
      similarity: Number(Number(row.similarity).toFixed(4)),
      age_delta_days: Number(Number(row.age_delta_days).toFixed(1)),
    })
    result.set(primaryId, neighbors)
  }

  return result
}

export interface SupersedingMemory {
  /** UUID of the most-recent superseder. */
  memory_id: string
  /** Kind of the superseder (decision/pattern/issue/note/...) */
  kind: string
  /** First 240 chars of content for inline display. */
  snippet: string
  /** Hops along the supersedes chain — 1 means direct supersession. */
  depth: number
}

/**
 * For each primary memory_id, walk the supersedes-edge chain forward
 * and return the *latest* (deepest non-superseded) replacement.
 *
 * Returns a Map keyed by primary. Primaries with no incoming
 * supersedes edges are absent from the map.
 *
 * Hard cap of 10 hops on the chain — supersession trees deeper than
 * that are pathological and would indicate cycle-breaker missing
 * upstream.
 */
export async function findSupersedingMemories(
  primaryMemoryIds: string[],
): Promise<Map<string, SupersedingMemory>> {
  const result = new Map<string, SupersedingMemory>()
  if (primaryMemoryIds.length === 0) return result

  // Edge semantics (per migration 014):
  //   from_memory_id 'supersedes' to_memory_id
  //   → from is the new replacement, to is the old (primary in our case).
  //
  // Walk forward: starting at primaries that appear as `to_memory_id`,
  // follow the chain by joining the next link's `to_memory_id` to the
  // current `from_memory_id`. The deepest replacement_id is the latest
  // version in the chain.
  const sql = `
WITH RECURSIVE chain AS (
    SELECT
        md.to_memory_id   AS primary_id,
        md.from_memory_id AS replacement_id,
        1 AS depth
      FROM devbrain.memory_dependencies md
      JOIN devbrain.memory m ON m.id = md.from_memory_id
     WHERE md.to_memory_id = ANY($1::uuid[])
       AND md.edge_type = 'supersedes'
       AND m.archived_at IS NULL
    UNION
    SELECT
        chain.primary_id,
        md.from_memory_id,
        chain.depth + 1
      FROM chain
      JOIN devbrain.memory_dependencies md
        ON md.to_memory_id = chain.replacement_id
       AND md.edge_type = 'supersedes'
      JOIN devbrain.memory m ON m.id = md.from_memory_id
     WHERE chain.depth < 10
       AND m.archived_at IS NULL
),
deepest AS (
    SELECT DISTINCT ON (primary_id)
           primary_id, replacement_id, depth
      FROM chain
     ORDER BY primary_id, depth DESC
)
SELECT
    d.primary_id::text     AS primary_id,
    d.replacement_id::text AS replacement_id,
    d.depth                AS depth,
    m.kind                 AS replacement_kind,
    m.content              AS replacement_content
  FROM deepest d
  JOIN devbrain.memory m ON m.id = d.replacement_id
 WHERE m.archived_at IS NULL
`
  const rows: QueryResult = await query(sql, [primaryMemoryIds])

  for (const row of rows.rows) {
    const content = String(row.replacement_content)
    result.set(String(row.primary_id), {
      memory_id: String(row.replacement_id),
      kind: String(row.replacement_kind),
      snippet: content.length > 240 ? content.slice(0, 240) + '…' : content,
      depth: Number(row.depth),
    })
  }

  return result
}

/**
 * Heuristic: should the consumer be warned that this primary may be
 * stale? True if the primary has been superseded OR if any recency
 * neighbor crosses BOTH a high-similarity and a meaningful-age-gap
 * threshold. The consumer (typically an LLM) is meant to treat the
 * warning as a nudge to verify currency, not as authoritative.
 */
export function computeRecencyWarning(
  superseder: SupersedingMemory | undefined,
  neighbors: RecencyNeighbor[] | undefined,
): boolean {
  if (superseder !== undefined) return true
  if (!neighbors || neighbors.length === 0) return false
  // Trigger when the closest neighbor is ≥0.55 similar AND ≥7 days
  // newer — strong signal that recent activity may have overtaken
  // this primary even without a formal supersedes edge.
  return neighbors.some(
    (n) => n.similarity >= 0.55 && n.age_delta_days >= 7,
  )
}
