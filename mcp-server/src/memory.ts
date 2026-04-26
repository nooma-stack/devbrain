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

export async function recordMemory(args: RecordMemoryArgs): Promise<void> {
  try {
    await query(
      `INSERT INTO devbrain.memory
           (project_id, kind, title, content, embedding, provenance_id)
       VALUES ($1, $2, $3, $4, $5::vector, $6)
       ON CONFLICT (provenance_id, kind) WHERE provenance_id IS NOT NULL
       DO NOTHING`,
      [
        args.projectId,
        args.kind,
        args.title ?? null,
        args.content,
        args.embeddingSql ?? null,
        args.provenanceId ?? null,
      ],
    )
  } catch (err) {
    // Best-effort: never let a memory failure surface to the caller.
    // The legacy write is the contract; this is shadow-write phase.
    console.error(
      `[memory] dual-write failed (kind=${args.kind}, provenance_id=${args.provenanceId ?? 'null'}): ${err}`,
    )
  }
}
