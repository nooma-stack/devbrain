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

## Behavior-preserving efficiency wins

- ✅ **#3 ingest one-txn-per-session** (PR #176). Embed all chunks up front, then
  write `raw_session + chunks + dual-writes` in ONE transaction. A crash now
  leaves the session atomically absent (hash gate re-ingests cleanly) instead of
  stranding a half-indexed session. db.py helpers take an optional `cur`;
  pipeline restarts the ingest watcher to pick up the new code. Atomicity test added.
- ✅ **#4 `backfill_chunks` ON CONFLICT** (PR #173). `ON CONFLICT
  (provenance_id, kind, md5(content)) DO NOTHING` — race-safe under the 045
  unique index, with a focused idempotency test (not the slow whole-table scan).
- ✅ **#5 cognify per-(pass,project) advisory lock** (PR #171). A manual
  `devbrain cognify` racing the scheduled run now skips instead of double-spending.
- ✅ **#6 slow-pass warning** (PR #171). Logs when a pass exceeds
  `DEVBRAIN_COGNIFY_SLOW_WARN_S`. (Hard kill deferred — needs a watchdog; the
  cites O(N×T) fix already removed the known offender.)

## Correctness bugs — all fixed

- ✅ **`with_graph=true` was silently dead** (PR #175). deep_search seeded the
  graph from `results.map(r => r.memory_id)`, but result objects lacked
  `memory_id` → empty seeds. Added `memory_id` to each result.
- ✅ **`extract` watermark poisoning** (PR #169). It read its own in-progress
  run-log row → `since=now` → 0 sessions every run; the pass had **never** once
  produced a row. Fixed to `completed_at IS NOT NULL AND error IS NULL`.
- ✅ **~22k decision/pattern atoms had no embedding** (PR #170). Not a generic
  "26%": the extract/curator atom-insert paths never embedded. Added a shared
  `cognify.embedding.embed_text`, wired into extract/curator/fanout, and
  backfilled all existing rows via `scripts/reembed_memory.py` (0 left).
- ✅ **Ollama `embed` no timeout** (PR #175). Added `AbortSignal.timeout` (30s).

## Deferred micro-optimizations (measure first — low ROI)

Not done deliberately: each is a speculative tweak on a now-working path; adding
indexes/complexity without a measured bottleneck has its own cost.

- **#7 `deep_search_graph_entry` single multi-seed CTE** (N walks → 1). Only
  matters now that `with_graph` works; revisit if graph latency shows up.
- **#8 `btree(project_id, created_at) WHERE archived_at IS NULL`** — adds
  write-time cost on every insert for an unmeasured read gain. Measure first.
- **#9 `findEarliestOnTopic` `DISTINCT ON`; `breadcrumb` seq via `MAX+1`** —
  micro-wins; the COUNT/overfetch costs are negligible at current scale.

## Open data follow-up (needs a go/no-go — LLM cost/time)

- **Extract backlog**: the extract pass was a no-op since it was wrapped by the
  run-log, so ~6,662 BrightBot sessions were never atomized. The code is fixed
  (forward), but catching up the backlog needs `cognify-bulk --project=brightbot`
  (codex backend = no token cost, but a multi-hour resumable job on the shared
  Studio). Flagged for a go-ahead, not auto-started.

## Notes for whoever picks this up

- All "do not optimize" invariants live in MEMORY_PIPELINE.md — read that first.
- Migrations are additive and idempotent; the runner (`factory/schema_migrate.py`)
  is advisory-locked + per-file-transactional and safe to re-run.
- Deploy to BrightBrain = land on `main`, then `git pull` on the Mac Studio
  (`/Users/lhtdev/devbrain`) and let the launchd jobs pick up the new code.
