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

#### ChatGPT desktop / Codex rollouts (2026-07-10)

OpenAI's unified ChatGPT desktop app (launched 2026-07-09, bundle
`com.openai.codex`) is the Codex app rebranded — desktop **Work/Codex
threads** are written as plaintext rollout JSONL to
`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (`originator: "Codex
Desktop"`), which the `codex` adapter parses. Notes:

- `~/.codex` is already in the codex adapter's `watch_paths`; desktop
  threads ingest with zero config. Cloud "Chat" tab conversations do NOT
  land locally (and ChatGPT Business has no Compliance/export API — that
  is Enterprise-only), so local capture is the only automatable path.
- Archived rollouts may be `*.jsonl.zst` (needs `zstandard`, in
  requirements). `event_msg` user/agent messages are parsed as a
  FALLBACK only — they duplicate `response_item` content in current
  builds.
- **Remote devs**: ship lighthouse-related rollouts to the Studio drop
  zone under `ingest-incoming/<dev>/codex/…` (the codex adapter claims
  that subtree; it is registered before claude_code so the greedy
  ".jsonl under ingest-incoming" match can't steal these). Because
  project attribution comes from `session_meta.cwd` (not the path),
  sync scripts MUST content-filter to work sessions, e.g.:

  ```bash
  # sync only rollouts whose cwd is under ~/lighthouse (privacy boundary)
  for f in $(grep -l '"cwd":"'"$HOME"'/lighthouse'       $HOME/.codex/sessions/*/*/*/rollout-*.jsonl 2>/dev/null); do
    rsync -az "$f" brightbrain-studio:ingest-incoming/$USER/codex/
  done
  ```

  The rollout format is undocumented and version-churny — parse
  failures should be watched after Codex app updates.

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

### Prompt-injection screening (`ingest/screening.py`, migration 049)
Rules, no model call, ~1 ms per transcript. Flags: `override` ("ignore
previous instructions", "you are now in developer mode", "new system
prompt:"), `agent_directive` ("if you are an AI reading this", "note to the
assistant:"), `fake_tag` (`<|im_start|>`, `[INST]`, `<<SYS>>`, a bare
`<system>` tag, `</tool_result>`, `\n\nHuman:`), `exfil` (send credentials to
an address, reveal the system prompt), `hidden_unicode` (Unicode tag
characters, bidi overrides, zero-width characters inside words).

- Every rule runs on the raw text, on a normalized form (HTML entities and
  `\uXXXX` / `%XX` escapes decoded, NFKC, invisible characters removed,
  Cyrillic/Greek look-alikes mapped to Latin, case-folded) and, for the
  wording rules, on a squashed form (separators collapsed, spelled-out
  letters rejoined). A hit only the normalized/squashed form produces also
  gets `obfuscated`.
- Trust comes only from structure and is opt-in. An adapter marks a
  message `principal` only when the person typed it (Claude Code: a user
  entry that is not `isMeta` and has no tool result; Codex: a user text
  item or desktop `user_message`); every other message, other adapters and
  raw_content JSON stored before the flag are untrusted. Even a principal
  message is trusted only up to the FIRST harness marker anywhere in it
  (`<system-reminder>`, `<task-notification>`, `<command-…>`, hook
  context, skill / CLAUDE.md / MCP-instruction blocks, pasted or mailed
  documents, `[tool_result]`, or any line starting with an XML-ish tag).
  The principal part only gets the hidden-unicode check. FLAT text
  (chunks, assembled prompts, summaries, notes) is never trusted: a role
  prefix there is just text a tool result could forge.
- Ingest screens each message and stores on each chunk the union of the
  flags of the messages it overlaps (`memory.screen_flags`, NULL = no
  flag), so a payload split across chunks flags them all; the summary is
  built from the messages with flagged ones withheld.
- extract and fanout screen the exact assembled prompt text (titles,
  contents and chunk joins included) and replace flagged segments with
  `[segment withheld by injection screening: <flags>]`; resummarize screens
  raw_content per message. A row with stored flags the rules no longer
  reproduce is withheld whole.
- Rows written through MCP are screened at write time (flags only, never
  blocked): `store` (the saved `title\n\ncontent`), `breadcrumb` /
  `start_session`, the `end_session` summary, and end_session lesson
  candidates. The MCP side (`mcp-server/src/screening.ts`) is a port of the
  flat-text path that reads the same `ingest/screening_rules.json`; both test
  suites run `ingest/screening_cases.json`, so a rule change that the two
  implementations disagree on fails CI.
- `deep_search` can select `memory.screen_flags` to label results.
- `devbrain screen-memory [--project] [--kind] [--since] [--apply]` reports
  (default, read-only) or sets flags on existing rows; it writes flagged rows
  only, never clean ones.
- `ingest.screening.model_check: true` adds a local-model pass over rule
  hits at ingest (loopback only unless `allow_remote_model`). It can only
  add (`model_confirmed`); a "benign" verdict clears nothing but a plain
  `agent_directive` hit, and each such veto is logged.

### Hosted backends: allow-list and codex isolation
- `cognify.allowed_backends` (`factory/cognify/backends.py`) lists the hosted
  backends a deployment may send memory/transcript text to: `codex` (ChatGPT
  login) and `anthropic` (Anthropic API). Unset = both; `[]` = none. Checked
  in extract, fanout, edges and resummarize right before each call;
  fail-closed (non-list value → none, unknown names ignored) and every refusal
  is logged. A refused extract run moves neither the watermark nor the
  backlog; refused fanout/resummarize sessions stay eligible.
- Cognify runs `codex exec` with its own `CODEX_HOME`
  (`factory/cognify/codex_home.py`, default `~/.devbrain/codex-cognify`): its
  own login and a config.toml with no MCP servers, plugins, apps or
  browser/computer-use tools, so an injected transcript cannot reach the
  host user's write-capable tools. Every codex call checks the directory and
  refuses codex (falling back to an allowed backend) when it is missing or
  unsafe. `devbrain cognify-codex-home --init` writes it; the login is
  `CODEX_HOME=<dir> codex login --device-auth`.

### Extract gate (`factory/cognify/gate.py`)
Off by default (`cognify.extract_gate.mode: off | dry_run | on`, env
`DEVBRAIN_EXTRACT_GATE`). Before extract sends a unit (one `provenance_id`
group: a transcript's chunks, an end_session summary, a breadcrumb chain, a
stored atom) to the LLM, the gate decides with no LLM call:

| decision | when |
|----------|------|
| skip | the unit is a stored atom (`atom_source`); an end_session summary written by a listed CLI (`cli:closure-backfill`); a transcript under a listed path part (`/subagents/`, `support-runner`); fewer than `min_chars` (300) source characters; a transcript with no human turn |
| downgrade (run last, only if LLM budget is left) | transcript text more than 85% tool output; fewer than 25% of sampled source rows below 0.80 cosine to an older atom (`low_novelty`) |
| extract | otherwise |

`dry_run` logs `extract_gate: would skip …` lines and records counts, reasons
and up to 50 ids per action in the extract run's `cognify_run_log.metadata.gate`;
extraction is unchanged. In `on`, a skipped unit is not picked up again unless
new source rows arrive. `devbrain cognify-gate --project <slug> --last 200`
reports decisions on recent units, with each unit's atoms so far, read-only.

### Write-time relation check (`factory/cognify/relate.py`, migration 048)
Off by default (`write_path.relation_check.mode: off | shadow | on`). When on,
each new decision/pattern/issue atom is compared with its nearest live atoms in
the same project (cosine >= `threshold`, default 0.80) and the local model
labels it:

| verdict | effect |
|---------|--------|
| `same` | nothing new is kept; the existing atom gets `strength + 0.1` (cap 1.0) and `last_hit = now()` |
| `refines` | row kept + `refined_by` edge old -> new |
| `contradicts` | row kept + `supersedes` edge new -> old, depends_on dependents queued, log row `review_status = 'pending'` |
| `new` | plain write |

- **extract** checks synchronously before its INSERT (a batch pass can wait for
  the model); a `same` atom is never inserted.
- **store** and **end_session lesson candidates** write first and spawn
  `devbrain cognify-relate` detached, so tool latency is unchanged. A `same`
  verdict there **archives** the new row (never deletes it).
- Every check writes one `devbrain.memory_relation_log` row (ids, scores, model,
  latency — no memory text). `shadow` mode logs without acting.
- `refines`/`contradicts` below `edge_min_similarity` (0.85) are written as plain
  rows: at lower similarity the 7B model's non-`same` answers were mostly wrong.
- **PHI**: the model is the deployment's own Ollama (`summarization.url` /
  `.model` unless overridden). A non-loopback URL turns the check off unless
  `allow_remote_model: true` is set explicitly. There is no hosted-API path.
- Any failure (Ollama down, timeout, bad JSON) is a plain write. `hit_count` is
  not touched (it is graduation's violation signal).

Pending contradictions:
`SELECT * FROM devbrain.memory_relation_log WHERE review_status = 'pending';`

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
  Semantic dedup across sessions is the relation check's job (above), which is
  opt-in and archives rather than deletes.
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

- **Project-less sessions never reached memory (fixed 2026-10).** A
  session whose path matched no project mapping was stored with
  `raw_sessions.project_id` / `chunks.project_id` NULL; the memory dual-write
  needs a project, so its chunks were invisible to search, extraction and
  fan-out. Re-ingest never re-resolved the project (the content-grew UPDATE
  did not touch it, and an unchanged file was skipped by hash). Now the
  UPDATE fills a NULL project, the watcher attaches an already-ingested
  project-less file once a mapping resolves it, and
  `ingest/repair_projects.py` backfills: it skips paths the current
  `exclude_path_parts` reject, copies each chunk's stored embedding into the
  new memory row (never re-embeds) with its screening flags, and throttles
  (`--rows-per-second`). A prefix mapping for a bare workspace root no
  longer overrides `auto_project_roots` for the root's sub-folders.
- **Extract "empty" outage (2026-09-17 → 2026-10).** The codex model slug was
  retired for ChatGPT accounts; codex printed the 400 as
  `ERROR: {"type":"error",...}` and exited 1. `_salvage_last_json` took that
  JSON as the result, so every unit read as zero lessons (`failure: empty`)
  and the Anthropic fallback never ran. Salvage now only accepts objects with
  the schema's keys, codex errors are api failures (logged with the account's
  listed models), the slug is set once in `cognify.codex_model`, and the
  reasoning effort is pinned. Units past the LLM cap and api failures are
  carried to the next run (`metadata.backlog`) instead of falling behind the
  watermark, and dry runs no longer move it.
- **Extract re-extraction loop (fixed 2026-10).** `_sessions_since` counted
  extracted atoms (`tier='lesson'`, same `provenance_id`, created after the
  watermark), so every extracted unit became a candidate again on the next
  run. One 2.9k-char session summary was re-extracted in 229 separate hours
  into 2,628 atoms; ~98% of all atoms came from summaries and breadcrumb chains
  re-extracted this way, which is where most near-duplicate atoms came from.
  Candidates now count source rows only.
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
