# Handoff: embedding/recall fixes + billing rewire — actions for the nooma devbrain deployment

**Date:** 2026-08-27 · **From:** Claude (LHT/brightbrain side) · **For:** the codex agent operating the nooma Mac Studio devbrain
**Repo state:** everything referenced is already merged to `main` (PRs #182, #184, #185, #186). Your job is deployment + data work on nooma, not code changes — with one optional small feature noted in §6.

---

## TL;DR — ordered checklist

1. Settle the embedding runtime FIRST (final ollama version, one endpoint for all writers). Do nothing below until this is stable.
2. `git pull` in `~/devbrain`; `cd mcp-server && npm run build`; let MCP server processes cycle (new connections pick up new dist).
3. Re-embed the corpus: `python ingest/reembed_memory.py` (full corpus), then `python ingest/reembed.py` (legacy chunks).
4. Rebuild the HNSW index **serially** (SQL in §4). **Never `REINDEX CONCURRENTLY` a pgvector HNSW index.**
5. Run the recall verification procedure (§5) with a pinned query vector.
6. Before reloading the cognify launchd jobs: wire metered billing (§7). They are currently **unloaded on purpose**.

---

## 1. Why: what we found on the LHT deployment (2026-08-26/27)

A freshly-written, exactly-relevant memory was reproducibly missing from `deep_search` results.
Three independent layers, each real, each fixed:

1. **Embedding-runtime drift.** An ollama upgrade (0.21→0.32) changed snowflake-arctic-embed2's numeric
   output. Rows embedded pre-upgrade recomputed against their own text at cosine 0.987–0.997 (should be
   exactly 1.000). A ~1% wobble reorders results inside dense score bands.
2. **Defective HNSW graph from `REINDEX CONCURRENTLY`.** After a concurrent reindex, the graph's global
   best for a query was 0.504 while 0.53 rows existed, and the target row was unreachable even at
   `ef_search=400`. A plain **serial** `CREATE INDEX` (8.9 min, 305K rows × 1024d) restored sane traversal.
3. **`ef_search` default too shallow.** Even on the clean graph, pgvector's default (40) missed a true
   rank-#3 row from top-10. `ef_search=200` recovers it at 19ms (exact scan: 992ms). Now set per pool
   connection in `mcp-server/src/db.ts` (PR #186).

**Nooma's measured state (2026-08-26 ~21:50Z):** drift is *inverted* — old rows recompute at 1.00000
(serving runtime matches the historical corpus) but **rows written 2026-08-26 recompute at ~0.9949**:
your qwen3.8 testing wrote embeddings through a different generation (different ollama build or a second
endpoint). Small blast radius, but the same disease. Assume the HNSW/ef_search issues apply to nooma
identically — they are corpus-shape and pgvector-version properties, not machine-specific.

## 2. Step 1 — settle the runtime (do this before any data work)

- Pick the final ollama version. Note: **qwen3.8:27b requires ollama ≥ 0.32.15** (0.21 rejects the
  manifest; LHT verified 0.32.15). If you upgrade ollama for it, that IS a runtime change → re-embed after.
- Ensure **every writer uses one endpoint**: mcp-server, `ingest/`, `factory/cognify/` all read
  `DEVBRAIN_OLLAMA_URL` / yaml `summarization.url` + `embedding.url`. Kill any second ollama instance or
  hardcoded URL from the testing era — two generations coexisting silently is how nooma got its Aug-26 drift.
- Determinism facts (measured on LHT, useful for your verification): arctic-embed2 through `/api/embed`
  is deterministic within a runtime — identical output across repeated calls, forced model reloads
  (`keep_alive: 0`), and 6-way concurrent load. If you see different vectors for the same text, you have
  two runtimes, not nondeterminism.

## 3. Step 2 — deploy the merged code

```bash
cd ~/devbrain && git pull origin main
cd mcp-server && npm run build        # tsc — this is also the typecheck
```
- MCP server processes are spawned per session by supergateway; new sessions get the new dist
  automatically. Long-lived sessions keep old code until they cycle — fine.
- What you get: fanout pins the codex mini model + salvages JSON codex printed but didn't write (#182);
  configurable summarizer window, default 48K chars + explicit `num_ctx` (#184); `reembed_memory.py`
  (#185); `hnsw.ef_search=200` per pool connection (#186).

## 4. Steps 3–4 — re-embed, then rebuild the index

```bash
cd ~/devbrain/ingest
# full corpus is simplest and harmless (already-current rows rewrite identical vectors);
# --cutoff only supports BEFORE-dates. If you want an efficient nooma run, add a --since
# flag first (small patch) and target created_at >= 2026-08-26.
../.venv/bin/python reembed_memory.py          # devbrain.memory (what deep_search reads)
../.venv/bin/python reembed.py                 # legacy devbrain.chunks
```
Live-safe: row-wise updates, batch commits, readers never blocked. LHT throughput: ~23–25 rows/s.

Then rebuild the index (check the index name on nooma first: `\di devbrain.*embedding*`):
```sql
SET maintenance_work_mem = '1GB';
SET max_parallel_maintenance_workers = 0;   -- parallel build blows containerized shm limits (DiskFull)
CREATE INDEX idx_memory_embedding_hnsw2 ON devbrain.memory
  USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=128)
  WHERE archived_at IS NULL;                -- match the existing partial predicate
DROP INDEX devbrain.idx_memory_embedding_hnsw;
ALTER INDEX devbrain.idx_memory_embedding_hnsw2 RENAME TO idx_memory_embedding_hnsw;
```
- Serial build blocks **writes** to `devbrain.memory` for the build duration (~9 min at 305K rows on an
  M3 Ultra; nooma's corpus may differ). Reads unaffected. Background writers just queue.
- **Never `REINDEX CONCURRENTLY`** on this index — that's what produced the corrupted graph on LHT.
- Do the same for any HNSW index on `devbrain.chunks` if one exists.

## 5. Step 5 — verification procedure (don't skip)

The diagnostic that cracked this: **windowed/no-LIMIT vector queries are EXACT; `ORDER BY … LIMIT`
goes through HNSW.** A row present in one but missing from the other = index problem, not data.

1. Pick a recently-written memory row you know is relevant to some query phrase.
2. Embed the query ONCE and save the vector to a file — fresh embeds per run confound comparisons.
3. Exact rank (no LIMIT):
   ```sql
   WITH ranked AS (SELECT id, row_number() OVER (ORDER BY embedding <=> :vec) rn
                   FROM devbrain.memory WHERE embedding IS NOT NULL AND archived_at IS NULL)
   SELECT rn FROM ranked WHERE id = :target;
   ```
4. Index rank: same ORDER BY with `LIMIT 10`, at `SET hnsw.ef_search = 200`.
5. PASS = target appears in the LIMIT scan at (or near) its exact rank, and spot-checked old rows
   recompute against their own text at 1.00000 (embed the row's text per the conventions below and
   compare `1 - (embedding <=> :vec)`).

Embed-text conventions (what `reembed_memory.py` implements — needed for any manual spot-check):
`chunk`, `session_summary` → content only; every other kind → `title\ncontent` when titled, else content.

## 6. Related but separate: summarizer model (optional, recommended)

LHT upgraded ingest summaries qwen2.5:7b → **qwen3.8:27b** with measured wins: qwen2.5:7b *hallucinated
completion* of unfinished sessions (memory poison); qwen3.8 reported true partial state. 70s vs 10s per
summary — irrelevant for async ingest. If you adopt it on nooma:
- ollama ≥ 0.32.15 (see §2 — triggers the re-embed rule if you upgrade),
- `config/devbrain.yaml` → `summarization.model: qwen3.8:27b`, restart `com.devbrain.ingest`,
- the wider 48K-char window + `num_ctx` fix apply automatically from #184,
- **`format=json` structured calls to qwen3.8 return EMPTY under default thinking — pass `"think": false`.**

## 7. Before reloading cognify: billing (the jobs are unloaded on purpose)

The four cognify LaunchAgents (`extract`, `edges`, `resummarize`, `fanout`) were unloaded on BOTH studios
on 2026-08-24 because their plists embedded **subscription OAuth tokens** (`CLAUDE_CODE_OAUTH_TOKEN`) that
drained personal Claude Max plans (537× 429 rate-limit errors during the saturation week).
- `factory/cognify/_anthropic_auth.py` gives `ANTHROPIC_API_KEY` **first precedence** — zero code changes
  needed. LHT pattern: write the key to `~/.config/devbrain/cognify-billing.env` (chmod 600), strip the
  token from the plists, wrap `ProgramArguments` with
  `/bin/bash -lc 'set -a; . $HOME/.config/devbrain/cognify-billing.env; set +a; exec <original cmd>'`.
- Nooma has **no API key configured anywhere** (checked) — Patrick must supply the billing choice for
  nooma (personal Console key, or route more of cognify to local ollama).
- The codex-first path is fine to keep: nooma's codex auth is already `patrick@nooma.solutions` (Pro),
  and #182 fixed the failure loop (fanout was 62,503× falling back to Claude, double-paying every item).
- The old embedded tokens should be **revoked** (they also live in `.plist.pre-installer` copies).

## 8. Standing rules this encodes

1. **Any change to the embedding runtime or model ⇒ full corpus re-embed + serial HNSW rebuild + recall check.**
2. **Never `REINDEX CONCURRENTLY` a pgvector HNSW index.** Serial CREATE + rename swap.
3. **`ef_search` is set explicitly per connection** (200 here), never left to the default.
4. **One ollama endpoint for all writers.** Two generations coexisting is silent corpus corruption.
5. **No subscription OAuth tokens in scheduled jobs.** Metered credentials only.
6. Backlog idea worth building: persist `(ollama_version, model_digest)` alongside embeddings so
   generation drift is mechanically detectable instead of a forensic hunt.
