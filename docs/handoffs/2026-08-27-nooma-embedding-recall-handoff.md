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
6. Reload the cognify launchd jobs in **codex + ollama mode** — strip the OAuth tokens first (§7). No Claude key is required; Claude is an optional fallback.

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

## 6. Model roster: who does what (the codex + ollama + optional-Claude posture)

How summaries actually flow, so you know what each model owns:
- **In-session agents author the best summaries themselves**: `end_session` takes the agent's own
  summary text and stores it — no extra model call, and it supersedes the ollama summary in ranking.
- **qwen3.8 (ollama) summarizes every ingested transcript** at ingest time (new or changed content),
  regardless of whether end_session later arrives. It is canonical only for orphan sessions.
- Input is the **first `max_input_chars` (48K default ≈ 12–16K tokens), single pass** — no map-reduce;
  content past the window is unseen *by the summary* (the chunking/embedding pipeline still covers the
  full transcript for deep_search, so tail content remains findable — only the summary is head-biased).
  Multi-pass/head+tail summarization is an open roadmap item if long-session summaries matter more.
- **cognify resummarize** (Claude) upgrades settled orphan summaries — OPTIONAL; with no Claude
  credential it skips gracefully and qwen3.8 summaries stand. With qwen3.8 + the 48K window this is a
  defensible steady state (the pass existed to paper over qwen2.5:7b's weakness).

qwen3.8 adoption (recommended):
- ollama ≥ 0.32.15 (see §2 — upgrading ollama triggers the re-embed rule),
- `config/devbrain.yaml` → `summarization.model: qwen3.8:27b`, restart `com.devbrain.ingest`,
- the wider 48K-char window + `num_ctx` fix apply automatically from #184,
- **`format=json` structured calls to qwen3.8 return EMPTY under default thinking — pass `"think": false`.**

Codex model guidance (measured 2026-08-27, codex-cli 0.149.1, ChatGPT auth):
- `gpt-5.4-mini` (the pinned default) remains the right and effectively only mini-tier id —
  `gpt-5.6-mini`, `gpt-5.5-mini`, `gpt-5.6-codex`, `gpt-5.x-codex-mini` all 400 with
  "not supported when using Codex with a ChatGPT account".
- `gpt-5.6-sol` works but must NEVER be used for the schema tasks: its reasoning burns >13k tokens on
  trivial classifications and blows the exec timeout (this exact misconfiguration caused fanout's
  62,503-fallback storm). Re-probe newer mini ids occasionally.

## 7. Reloading cognify: codex + ollama mode, Claude as optional fallback (DECIDED posture for nooma)

The four cognify LaunchAgents (`extract`, `edges`, `resummarize`, `fanout`) were unloaded on BOTH studios
on 2026-08-24 because their plists embedded **subscription OAuth tokens** (`CLAUDE_CODE_OAUTH_TOKEN`) that
drained personal Claude Max plans (537× 429 rate-limit errors during the saturation week).

**Nooma runs WITHOUT a Claude credential.** This works because `factory/cognify/_anthropic_auth.py` is
fail-open: with no credential, Claude-dependent work **skips gracefully** instead of erroring —
- extract/fanout: codex (ChatGPT sub, `patrick@nooma.solutions` Pro — verified) handles everything;
  a codex-failed item is skipped and retried on a later pass (#182 made codex failures rare);
- resummarize: skips entirely; qwen3.8 summaries stand as canonical for orphan sessions (see §6).

**To reload:**
1. Strip `CLAUDE_CODE_OAUTH_TOKEN` from all four plists (plistlib, keep `.bak` copies) — do NOT add any
   replacement credential.
2. `launchctl load` the four plists; kickstart extract once and confirm `[ok]` lines in
   `~/.devbrain/logs/cognify-extract.log` with no auth errors.
3. **Revoke the old tokens** (they also live in `.plist.pre-installer` copies).

**To enable the Claude fallback later** (optional): put `ANTHROPIC_API_KEY=<console key>` in
`~/.config/devbrain/cognify-billing.env` (chmod 600) and wrap each plist's `ProgramArguments` with
`/bin/bash -lc 'set -a; . $HOME/.config/devbrain/cognify-billing.env; set +a; exec <original cmd>'`
(the LHT pattern). `ANTHROPIC_API_KEY` has first precedence — no code changes. Model defaults are now
**claude-sonnet-5** (env-overridable: `DEVBRAIN_EXTRACT_FALLBACK`, `DEVBRAIN_FANOUT_FALLBACK`,
`DEVBRAIN_RESUMMARIZE_MODEL`).

## 8. Standing rules this encodes

1. **Any change to the embedding runtime or model ⇒ full corpus re-embed + serial HNSW rebuild + recall check.**
2. **Never `REINDEX CONCURRENTLY` a pgvector HNSW index.** Serial CREATE + rename swap.
3. **`ef_search` is set explicitly per connection** (200 here), never left to the default.
4. **One ollama endpoint for all writers.** Two generations coexisting is silent corpus corruption.
5. **No subscription OAuth tokens in scheduled jobs.** Metered credentials only.
6. Backlog idea worth building: persist `(ollama_version, model_digest)` alongside embeddings so
   generation drift is mechanically detectable instead of a forensic hunt.

## 9. Roadmap: embedding-model upgrade (evaluate, don't rush)

snowflake-arctic-embed2 (1024d) is solid but no longer the frontier: the **Qwen3-Embedding family**
(0.6B/4B/8B, ollama-native, MTEB-eng-v2 70.7 even at 0.6B) is the current local standout, and it supports
**user-defined output dimensions (32–1024, MRL)** — meaning it can run at 1024 dims and drop into the
existing `vector(1024)` schema with NO column migration. A swap is a full migration by definition
(different model = different space): run the §4 runbook end-to-end — re-embed everything, serial index
rebuild, pinned-vector recall verification. Budget: re-embed is model-bound (a 4B embedder is several×
slower than arctic's 568M; LHT took ~5h at 23 rows/s on arctic — plan for most of a day at 4B).
Note Qwen3-Embedding is instruction-aware: bare symmetric embedding (what devbrain does) works, but
query-side instruction prefixes are where its last few retrieval points live — a later enhancement.
Do this AFTER the current stabilization has soaked; coordinate LHT + nooma so the two DBs don't sit on
different embedding models long-term.


## 10. Session-closure system (added 2026-08-29) — deploy on nooma too

Most sessions never call `end_session` (6,500+ unclosed historical claude_code sessions on the LHT
DB alone), leaving qwen summaries of only the transcript HEAD as their memory. The closure system
fixes this with an author-quality ladder. Components (all in-repo):

- **`hooks/session-checkpoint/`** — a `SessionStart(matcher: compact)` hook: right after every
  compaction the model is instructed to persist a `CHECKPOINT pre-compact:` breadcrumb of the arc it
  just compacted, while it still holds the summary. Install per machine (script + settings snippet in
  the folder). Long sessions thus self-document as a chain of in-context checkpoints — the rolling
  ledger, authored by the model that lived it, at near-zero marginal cost.
- **`ingest/session_closure.py`** — mechanical closure detection: a closed session carries an
  `mcp__*__end_session` tool_use in its own transcript (no sentinel convention needed — the tool call
  IS the sentinel, and it works retroactively on every old transcript). The same scan extracts the
  `conversation_uuid` chain linkage and the last-breadcrumb position.
- **`ingest/close_orphan_sessions.py`** — scanner + backfill worker. `--report` scans; backfill
  summarizes only the tail from the last checkpoint (head+tail capped), then calls the REAL MCP
  `end_session` via `ingest/mcp_stdio_client.py`, so logging (`end_session_log.cli =
  'closure-backfill'`), enrichment, embedding, and fanout all happen exactly as for a live agent.
  Backends, best first: `resume` (cold-`claude --resume`; the original model closes its own session),
  `codex` (gpt-5.6-luna/terra via stdin — verified reachable on the ChatGPT sub; use for BOUNDED
  backfills, it is an interactive subscription), `openrouter` (ZDR + data_collection=deny enforced in
  the request; pennies; steady-state default for non-PHI), `ollama` (local, $0, the floor).
- **PHI guard**: remote backends refuse without `DEVBRAIN_CLOSURE_REMOTE_OK=1`. On machines whose
  transcripts may contain clinical content, leave it unset (ollama/resume only). Nooma personal
  content: setting it is fine.

Nooma rollout: install the hook, run `--report`, then work the backlog —
`--backend codex --model gpt-5.6-luna --limit 20` for a bounded historical pass (watch your ChatGPT
sub limits), then schedule the scanner (launchd, hourly) with `--backend ollama --limit 5` as the
steady state. Compliance summary for backend choices: HIPAA PHI requires a **BAA** — ZDR alone is not
sufficient (Vertex under a GCP BAA, or local, for PHI). Non-PHI: OpenRouter one-click ZDR +
data_collection=deny, or Ollama Cloud (states transient processing, no logging/training/retention),
or HF Inference Endpoints (no payload storage; 30-day logs; BAA available on Enterprise).
