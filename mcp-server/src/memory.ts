// Adapter helper for dual-writing into devbrain.memory (P2.b).
//
// Reads still go to the legacy tables. Writes go to BOTH the legacy
// table and devbrain.memory; this helper handles the unified-table
// side. Failures are logged and swallowed — the legacy write that
// already happened (or is about to happen) remains the source of
// truth, exactly like ingest/memory_writer.py does on the Python side.
//
// No SAVEPOINT here, by design: pg.Pool gives each query() its own
// connection, so an error on this call cannot poison anyone else's
// transaction. The Python adapter needs a savepoint because psycopg2
// runs the dual-write inside the caller's open transaction; pg.Pool
// doesn't have that constraint.
//
// Idempotency: relies on migration 011's partial unique index
// (idx_memory_provenance_kind_unique on (provenance_id, kind) WHERE
// provenance_id IS NOT NULL). Two concurrent dual-writes for the same
// legacy row collapse to one memory row via ON CONFLICT DO NOTHING.

import { query } from './db.js'

export type MemoryKind =
  | 'chunk'
  | 'decision'
  | 'pattern'
  | 'issue'
  | 'session_summary'

export interface RecordMemoryArgs {
  /** memory.project_id is NOT NULL — caller must resolve before calling. */
  projectId: string
  kind: MemoryKind
  content: string
  /** Optional human-friendly title. Chunks/sessions normally pass undefined. */
  title?: string | null
  /**
   * pgvector literal already formatted as `[v1,v2,…]`. We never
   * recompute embeddings — pass the legacy row's vector verbatim.
   */
  embeddingSql?: string | null
  /**
   * Legacy row's UUID. If undefined/null no dedup is enforced (the
   * partial unique index has WHERE provenance_id IS NOT NULL).
   */
  provenanceId?: string | null
}

export async function recordMemory(args: RecordMemoryArgs): Promise<string | null> {
  try {
    // Single round-trip: try to insert; on conflict (existing row for this
    // provenance), fall through to a SELECT for the existing id. The CTE
    // approach is cheaper than insert-then-select-on-conflict and handles
    // the no-provenance case too (when provenance_id is NULL the partial
    // unique index doesn't fire, so the INSERT path always succeeds).
    const result = await query<{ id: string }>(
      `WITH inserted AS (
         INSERT INTO devbrain.memory
             (project_id, kind, title, content, embedding, provenance_id)
         VALUES ($1, $2, $3, $4, $5::vector, $6)
         ON CONFLICT (provenance_id, kind) WHERE provenance_id IS NOT NULL
         DO NOTHING
         RETURNING id
       )
       SELECT id FROM inserted
       UNION ALL
       SELECT id FROM devbrain.memory
       WHERE provenance_id = $6 AND kind = $2 AND NOT EXISTS (SELECT 1 FROM inserted)
       LIMIT 1`,
      [
        args.projectId,
        args.kind,
        args.title ?? null,
        args.content,
        args.embeddingSql ?? null,
        args.provenanceId ?? null,
      ],
    )
    return result.rows[0]?.id ?? null
  } catch (err) {
    // Best-effort: never let a memory failure surface to the caller.
    // The legacy write is the contract; this is shadow-write phase.
    console.error(
      `[memory] dual-write failed (kind=${args.kind}, provenance_id=${args.provenanceId ?? 'null'}): ${err}`,
    )
    return null
  }
}

/**
 * Resolve a UUID to a `devbrain.memory.id`.
 *
 * Accepts either:
 *   - a memory.id directly (returns it unchanged if it exists), or
 *   - a legacy provenance UUID (decision/pattern/issue id), in which case
 *     we look up the corresponding memory row.
 *
 * Returns null on miss. Used by the `store` tool to wire up `depends_on`
 * and `supersedes` edges from agent-supplied UUIDs that may have come
 * from search results pointing at either the memory or the legacy table.
 */
export async function resolveMemoryId(uuid: string): Promise<string | null> {
  try {
    const result = await query<{ id: string }>(
      `SELECT id FROM devbrain.memory WHERE id = $1
         UNION ALL
       SELECT id FROM devbrain.memory WHERE provenance_id = $1
       LIMIT 1`,
      [uuid],
    )
    return result.rows[0]?.id ?? null
  } catch (err) {
    console.error(`[memory] resolveMemoryId(${uuid}) failed: ${err}`)
    return null
  }
}

/**
 * Insert a typed dependency edge between two memory rows.
 *
 * Idempotent via the (from_memory_id, to_memory_id, edge_type) unique
 * constraint. Caller responsibility: ensure both memory IDs are valid
 * (use resolveMemoryId first); silently skips self-loops and handles
 * conflicts as no-ops. Best-effort like recordMemory — failures are
 * logged but never raised to the caller.
 */
export async function recordMemoryDependency(args: {
  fromMemoryId: string
  toMemoryId: string
  edgeType: 'cites' | 'depends_on' | 'supersedes' | 'contradicts'
  confidence?: number
  createdBy?: string
  metadata?: Record<string, unknown>
}): Promise<void> {
  if (args.fromMemoryId === args.toMemoryId) return
  try {
    await query(
      `INSERT INTO devbrain.memory_dependencies
           (from_memory_id, to_memory_id, edge_type, confidence, created_by, metadata)
       VALUES ($1, $2, $3, $4, $5, $6)
       ON CONFLICT (from_memory_id, to_memory_id, edge_type) DO NOTHING`,
      [
        args.fromMemoryId,
        args.toMemoryId,
        args.edgeType,
        args.confidence ?? 1.0,
        args.createdBy ?? 'mcp:store',
        args.metadata ? JSON.stringify(args.metadata) : null,
      ],
    )
  } catch (err) {
    console.error(
      `[memory] recordMemoryDependency(${args.edgeType}, ${args.fromMemoryId}→${args.toMemoryId}) failed: ${err}`,
    )
  }
}
