# Memory pipeline efficiency & correctness roadmap (2026-06-25)

Tracked off a full-stack audit of the DevBrain/BrightBrain memory system
(ingest → cognify → retrieval → graph → curator → ops). Goal: make each layer
accomplish the **same result more efficiently** without breaking intentional
functions. See [../MEMORY_PIPELINE.md](../MEMORY_PIPELINE.md) for the architecture
map and the "do not optimize these" list.

Legend: ✅ shipped · 🔜 queued · 🧪 needs clean-DB validation · 🐛 correctness (changes wrong behavior).

## Shipped

- ✅ **Chunk dual-write dedup** (migration 045 + `memory_writer.py` ON CONFLICT).
  Root cause of `deep_search` flagging "stale by 75+ days" on every query:
  the chunk dual-write had no `ON CONFLICT`, so re-running the backfill
  duplicated every chunk (BrightBrain hit 83% dup chunk rows, one chunk 824×).
  Fixed; live BrightBrain cleaned 341k→84k rows, ivfflat reindexed.
- ✅ **#1 `edges` cites pass efficiency** (PRs #165 + #166). The O(N×T) regex
  double-loop re-compiled patterns past Python's 512-entry regex cache and
  pegged a core ~36h. Fixed in two behavior-preserving passes: #165 = load
  memory **once** (was read twice), **pre-compile** patterns, substring
  pre-filter, contradiction-walk early-exit. #166 = **first-word inverted
  index** (a `\bTITLE\b` match needs TITLE's first word present as a whole word,
  so only test titles in that bucket) → O(rows × content-words). Verified on the
  Studio: the cites scan over 84k rows × 21k titles went **36h-pegging → 74s**.
  A randomized equivalence test asserts byte-identical edges vs the original.
- ✅ **#2 embedding index → HNSW** (migration 046, PR #167). NOTE: the original
  "ivfflat caps at 63% recall, HNSW ~95%" claim was a **measurement artifact** —
  an id-overlap metric mis-read tied/duplicate embeddings as misses. The
  tie-robust metric shows ivfflat (probes=10) AND HNSW (ef_search=40) both at
  **distance-recall@10 = 1.000, ~1ms p50** on live data. Retrieval was never
  broken. Switched to HNSW anyway for the marginal wins at equal recall/latency:
  ~half the size (337 vs 656 MB) and no `probes` tuning. ivfflat.probes reset.

## Behavior-preserving efficiency wins (queued)

- 🔜 **#3 ingest one-txn-per-session**: keep embedding *outside* the txn, then
  write `raw_session + all chunks + dual-write` in ONE per-session transaction
  with `execute_values` (today: a new connection + commit per chunk). Session
  size profile checked — max 660 chunks / 1.1 MB, only 3 sessions >500, none
  >1000 — so no commit boundary needed. This flips the invariant from
  "partially-visible session that can't self-complete" (the hash gate skips a
  crashed session on retry) to "atomically present or absent" — strictly better
  for recall completeness and extract fidelity. Only cost: a mid-session crash
  re-ingests from scratch (rare). No commit boundary unless giant sessions appear.
- 🧪 **#4 `backfill_chunks` ON CONFLICT**: now that migration 045's unique index
  exists, `backfill_chunks`' plain INSERT raises a unique violation on a dup and
  the batch-failure path loses the whole batch's good inserts. Add
  `ON CONFLICT (provenance_id, kind, md5(content)) … DO NOTHING` to match the
  live path. NOTE: split out of the #1 PR — the existing
  `test_backfill_chunks_inserts_to_memory` runs a slow (~4 min) whole-table
  backfill and is environment-sensitive on a populated laptop DB; do this with
  a focused clean-DB test and address the integration test's full-table scan.
- 🔜 **#5 cognify per-(pass,project) advisory lock** (mirror the migration
  runner): stops a manual `devbrain cognify` from double-spending LLM while the
  scheduled run is mid-flight (correctness is saved today only by `ON CONFLICT`).
- 🔜 **#6 circuit breaker on passes**: pre-load row-count guard + wall-clock cap
  so a single pegged pass can't silently starve its launchd cadence again.
- 🔜 **#7 `deep_search_graph_entry` single multi-seed CTE** instead of N
  sequential `walk()` calls (3N → ~3 round-trips), reproducing the min-hop dedup.
- 🔜 **#8** add `btree(project_id, created_at) WHERE archived_at IS NULL` for the
  recency-neighbor / project-context queries now doing sort/ANN-filter.
- 🔜 **#9 minor**: `findEarliestOnTopic`/recency LATERAL `DISTINCT ON` to cut
  transfer; `breadcrumb` seq via `MAX+1` in the INSERT (avoid per-call COUNT).

## Correctness bugs (change wrong behavior — schedule deliberately)

- 🐛 **`with_graph=true` is silently dead**: `deep_search` reads `r.memory_id`
  from the result objects, which never set that field, so `seedIds` is always
  empty and the graph-enrichment subprocess never runs (`index.ts:668` vs the
  correct `top[i].memory_id` at `:632`).
- 🐛 **`extract` watermark poisoning**: `_last_successful_run` keys on
  `error IS NULL`, but the run-log row is committed with NULL error *at start*
  (`orchestrator.py:239`). A crashed/in-progress run looks "successful" → its
  sessions are never re-extracted (silent memory loss) or a gap opens under
  overlap. Fix: predicate on `completed_at IS NOT NULL AND error IS NULL`.
- 🐛 **~26% of non-archived memory rows have no embedding** (≈65,850 / 89,372)
  → invisible to `deep_search`. Identify which kinds (likely extract-inserted
  lessons/decisions) — likely a backfill/embed gap.
- 🐛 **Ollama `embed` has no timeout** → any tool call (search/store/breadcrumb)
  hangs indefinitely if Ollama stalls. Add `AbortSignal.timeout()`.

## Notes for whoever picks this up

- All "do not optimize" invariants live in MEMORY_PIPELINE.md — read that first.
- Migrations are additive and idempotent; the runner (`factory/schema_migrate.py`)
  is advisory-locked + per-file-transactional and safe to re-run.
- Deploy to BrightBrain = land on `main`, then `git pull` on the Mac Studio
  (`/Users/lhtdev/devbrain`) and let the launchd jobs pick up the new code.
