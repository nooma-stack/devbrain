# DevBrain / BrightBrain Memory Pipeline — full-stack map

> Companion to [MEMORY_MODEL.md](MEMORY_MODEL.md) (which covers the `devbrain.memory`
> table itself). This doc maps how the **whole** system writes, maintains, and
> recalls memory across its layers — so a change in one layer doesn't silently
> break an intentional function in another.
>
> Same codebase runs as **devbrain** (laptop, `DEVBRAIN_PROJECT=devbrain`) and
> **brightbrain** (Mac Studio, `lhtdev`, Postgres in docker `devbrain-db`).

## Three pipelines over one store

Everything is `devbrain.memory` (unified table, 1024-dim pgvector embeddings) plus
a typed edge graph (`devbrain.memory_dependencies`) and a hash-chained audit ledger
(`devbrain.memory_ledger`).

### A. Ingest / "memify" (write path)
Always-on `com.devbrain.ingest` launchd watcher → `watchdog` sees a transcript in a
watched dir / `~/ingest-incoming` drop-zone → adapter parses it → `chunk_text`
(1600-char windows, 320 overlap) → `embed_batch` via local Ollama
(`snowflake-arctic-embed2`, 1024-dim) → writes `raw_sessions` (lossless transcript) +
`chunks` + **dual-writes** one `devbrain.memory` row per chunk.

- Entry: `ingest/main.py:189` (`watch`), `ingest/pipeline.py:68` (`_process_session`).
- Dual-write: `ingest/memory_writer.py:36` (`record_memory`), per-kind `ON CONFLICT`.
- `provenance_id` for a chunk = the **source session UUID** (`chunks.source_id`,
  migration 032), NOT the chunk's own id — so a session's many chunks share one
  provenance and the content hash is the distinguishing key.

The direct MCP tools (`store`, `start_session`, `breadcrumb`, `end_session`) bypass
ingest and write `memory` straight from the TS server (`mcp-server/src/index.ts`),
its own `pg.Pool`, its own Ollama embed call.

### B. Cognify (knowledge maintenance)
Seven scheduled launchd passes, orchestrated by `factory/cognify/orchestrator.py`
(`PASS_ORDER`), each logged to `cognify_run_log` / `cognify_spend_log`:

| pass | cadence | LLM? | mutates |
|------|---------|------|---------|
| `decay` | hourly | no | `memory.strength` (two-pass 90d/30d) |
| `gc` | weekly | no | `memory.archived_at` (**never deletes** — HIPAA) |
| `extract` | hourly | 1/session, cap 20 | lessons→`pattern`, decisions→`decision` |
| `edges` | 6h | contradicts cap 15; cites zero-LLM | `memory_dependencies` (cites/contradicts) |
| `strengthen` | daily | no | rule graduate/demote |
| `fanout` | hourly | 1/session, **no cap** | cross-project `session_summary` rows |
| `resummarize` | hourly | 1/session Sonnet, **no cap** | upgrades Ollama summaries |

The **curator** runs at `end_session` (`factory/curator/end_session.py`): applies the
agent's volunteered judgment (promote / contradict / refine / new-edges) and drains
`curator_re_eval_queue`, decaying `strength` along `depends_on` cascade edges with a
subtractive penalty and 24h-half-life freshness.

### C. Retrieval (read path) — `deep_search`
`mcp-server/src/index.ts:281`: embed query → ivfflat ANN over `memory` (filtered
`project_id` / `kind` / `archived_at`) → annotate each hit via `recency.ts`
(recency-neighbors, supersedes forward-walk, earliest-on-topic,
`primary_age_days` / `recency_warning`) → optional graph enrichment (Python
`factory/graph/walker.py` recursive-CTE, hop/node capped, via subprocess) →
raw-transcript drill-down from `raw_sessions`.

## Cross-cutting

- **Ledger**: every `memory` write fires an AFTER trigger writing a SHA-256
  hash-chain row (`memory_ledger`), serialized by a `pg_advisory_xact_lock` for
  tamper-evidence.
- **Idempotency** is content-natural-key everywhere (`ON CONFLICT` / anti-join),
  NOT app-level locks. The only pipeline serialization is **launchd one-instance-
  per-label** + time.
- **Identifiers**: `provenance_id` (loose pointer, no FK — spans tables);
  `conversation_uuid` chains `start_session`→`breadcrumb`→`end_session`.

## Intentional designs — do NOT "optimize" these

Each encodes a past incident or invariant:

- **Best-effort dual-write that swallows errors** (`memory_writer.py` SAVEPOINT,
  `memory.ts`) — legacy tables are source of truth; a memory failure must never
  roll back the legacy write. (TS side needs no savepoint — `pg.Pool` is
  connection-per-query.)
- **`gc` archives, never deletes; `decay` clamps strength to 0.001; `created_at`
  preserved on backfill** — HIPAA audit trail.
- **`memory_ledger` has no FK; `provenance_id` is a loose pointer;
  `curator_re_eval_queue.cascade_source_id` is RESTRICT** — ledger outlives
  deleted rows; cascade source is the audit anchor.
- **Dedup indexes are provenance/content-scoped; `idx_memory_import_dedup` is
  deliberately non-unique** — identical text from *different* sessions is kept.
- **Never reinstate migration 011's `(provenance_id, kind)` unique index** — an
  operator did and hard-deleted 76k rows (2026-05-28). `backfill_memory._ensure_schema`
  asserts the 037 index *specifically* to prevent this.
- **codex-first → Sonnet fallback; no assistant-prefill on the OAuth path;
  per-call client recycle** — each defends a real LLM failure mode.
- **`supersedes` auto-walk in `recency.ts`** — writers don't reliably set edges,
  so deep_search annotates staleness rather than trusting them.
- **`end_session` always invokes the curator** (even empty judgment) — guarantees
  an `end_session_log` row and a cascade-queue drain. Cross-project payloads are
  **wholesale-rejected** (P_end_session_isolation).
- **Subtractive (not multiplicative) cascade penalty; 24h half-life; decay
  two-pass order** — tuned so multi-hop cascades don't zero memories by depth.
- **import's post-commit conditional `REINDEX`; recency 5×/50× overfetch** — load-bearing.

## Known seams that have bitten us

- The chunk dual-write had **no** `ON CONFLICT` until migration 045 — re-running
  the backfill duplicated every chunk (BrightBrain hit 83% dup chunk rows, one
  chunk 824×), flooding `deep_search`'s top-K with stale duplicates that tripped
  `recency_warning` on every query. Fixed: `idx_memory_chunk_dedup_unique` +
  `ON CONFLICT (provenance_id, kind, md5(content))`.
- `edges._detect_cites` was an O(N×T) regex double-loop (every memory row × every
  distinct title) that re-compiled patterns past Python's 512 regex cache — it
  pegged a core for ~36h on the bloated table. See the efficiency roadmap.

See [plans/2026-06-25-memory-efficiency-roadmap.md](plans/2026-06-25-memory-efficiency-roadmap.md)
for the prioritized efficiency/correctness work tracked off a full-stack audit.
