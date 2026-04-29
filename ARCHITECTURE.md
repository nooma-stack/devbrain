# DevBrain Architecture

> This document describes **DevBrain v0.1** — the reality of what's in this
> repo today. Forward-looking work is marked explicitly and links to the
> [hardening plan](docs/plans/2026-04-15-hardening-plan.md).

---

## 1. Overview

**DevBrain** is a local-first persistent memory and autonomous dev factory
for coding agents. It gives AI coding tools (Claude Code, Codex, Gemini CLI,
and any MCP client) a shared brain that survives across sessions, across
models, and across apps.

It is built for two audiences:

- **Coding-agent builders** who want their agents to remember decisions,
  patterns, and bug fixes without re-reading entire repos every session.
- **Teams** who want a shared memory and a disciplined, auditable pipeline
  for turning feature specs into reviewed, QA'd pull requests.

It exists because agents today lose context at every turn. Every session
starts cold. Every new model re-learns the same architecture. Every
developer reinvents the same automation. DevBrain fixes this by persisting
sessions to Postgres, making them searchable via embeddings, and exposing
the whole thing through MCP.

The long-term vision is a three-pillar system — **memory + knowledge +
discipline**. v0.1 ships the first pillar (persistent memory) plus a dev
factory orchestrator. The knowledge graph and discipline/eval layers are
planned for later phases; see
[§9 What's intentionally NOT in v0.1](#9-whats-intentionally-not-in-v01).

---

## 2. Principles

1. **Local-first.** Your code, transcripts, and decisions never leave the
   host unless you explicitly configure a remote channel. Postgres and
   Ollama both run locally.
2. **Model-agnostic.** Any MCP-capable client works. The factory can route
   different phases to different CLIs (claude, codex, gemini). Embeddings
   are pluggable (Ollama is the default).
3. **App-agnostic.** Sessions from Claude Code, Codex, Gemini CLI,
   OpenClaw, and generic Markdown memory files all land in the same
   schema via adapters.
4. **MCP-native.** DevBrain's public surface is a single MCP server with
   14 tools. CLIs, dashboards, and cron jobs all go through the same
   layer that agents do.
5. **Postgres-only backend.** Relational data, JSONB metadata, and pgvector
   embeddings all live in one database. No separate vector store, no
   separate cache, no separate queue.
6. **Opinionated over pluggable.** Chunk size, embedding dimensions (1024),
   the state machine, and the 5-phase factory pipeline are fixed. Adapters
   and notification channels are the two designed extension points.

---

## 3. Component diagram

```
                    ┌──────────────────────────────┐
    MCP clients ───▶│  MCP Server  (TypeScript)    │
  (claude / codex / │  14 tools over stdio         │
   gemini / custom) │  mcp-server/src/index.ts     │
                    └──────────────┬───────────────┘
                                   │
                                   ▼
   ┌──────────────────┐   ┌──────────────────────┐   ┌─────────────────┐
   │ Ingest Pipeline  │──▶│  PostgreSQL 17       │◀──│ Factory         │
   │ (Python, watch + │   │  + pgvector          │   │ Orchestrator    │
   │  launchd)        │   │  schema: devbrain    │   │ (Python, spawn) │
   │                  │   │                      │   │                 │
   │ 5 adapters       │   │  ~12 tables, 1024-d  │   │ state machine + │
   │ chunker + embed  │   │  embeddings, JSONB   │   │ cleanup agent   │
   │ ingest/          │   │  migrations/*.sql    │   │ factory/        │
   └──────────────────┘   └──────────────────────┘   └────────┬────────┘
            │                      ▲                          │
            │                      │                          ▼
            ▼                      │               ┌────────────────────┐
   ┌──────────────────┐            │               │ Notification       │
   │ Ollama (host)    │            │               │ Router (8 channels)│
   │ - embedding      │            │               │ tmux/smtp/gmail/   │
   │ - summarization  │            │               │ gchat/telegram/    │
   └──────────────────┘            │               │ slack/discord/http │
                                   │               └─────────┬──────────┘
                                   │                         │
            ┌──────────────────────┴────┐                    │
            │ CLI  (bin/devbrain)       │                    │
            │ dev registration, notify, │                    │
            │ instance admin            │                    │
            │ factory/cli.py            │                    │
            └───────────────────────────┘                    │
                                                             ▼
                                                    (dev tmux pane,
                                                     inbox, chat, etc.)
```

**Runtime dependencies:**

- **PostgreSQL 17 + pgvector** (Docker via `docker-compose.yml`).
- **Ollama** running natively on the host for Metal/CUDA embedding and
  summarization (default models: `snowflake-arctic-embed2` @ 1024 dims,
  `qwen2.5:7b` for summaries).
- **AI CLIs** invoked by the factory as subprocesses: `claude`, `codex`,
  `gemini`. The factory executes them in the project's repo and captures
  stdout as artifacts.
- **launchd** on macOS to keep the ingest watcher running.

---

## 4. Data model

All tables live in the `devbrain` schema
(see [`migrations/001_initial_schema.sql`](migrations/001_initial_schema.sql)
and migrations 002–006).

### Core memory tables

| Table | Purpose |
|-------|---------|
| `projects` | Registry of projects DevBrain tracks. Slug, root path, tech stack, lint/test commands, constraints. |
| `raw_sessions` | **Lossless** transcript storage. One row per ingested session file, keyed on `(source_app, source_hash)`. Holds the full parsed JSON (USF format) as `raw_content`. |
| `chunks` | The vector-searchable unit. 1024-dim embeddings, plus `source_type` + `source_id` + `source_line_start/end` so every chunk can drill back to its raw context. |
| `decisions` | Structured architecture decisions: title, context, decision, rationale, alternatives, supersession chain. |
| `patterns` | Reusable code/architecture patterns: name, category, description, example_code, tags. |
| `issues` | Bugs worth remembering: title, root cause, fix applied, prevention. |
| `codebase_index` | One row per indexed source file: summary, imports, exports, embedding, last commit hash. |

Everything that can be searched gets an embedded twin in `chunks`. When you
`store` a decision, the orchestrator also writes a `chunks` row pointing at
the decision's `id` — so `deep_search` retrieves it alongside raw session
chunks.

### Factory tables

| Table | Purpose |
|-------|---------|
| `factory_jobs` | The job itself: spec, status, current_phase, branch_name, error_count, assigned_cli, submitted_by, blocked_by_job_id, blocked_resolution, archived_at. |
| `factory_artifacts` | One row per phase output: plan docs, diffs, review reports, QA reports. Includes `findings_count` / `blocking_count`. |
| `factory_cleanup_reports` | Post-run summaries and recovery-attempt reports from the cleanup agent. |
| `file_locks` | Per-project, per-file advisory locks. `UNIQUE (project_id, file_path)`. Default 2h expiry so crashed jobs can't deadlock the system. |

### Developer & notification tables

| Table | Purpose |
|-------|---------|
| `devs` | Developer registry keyed by `dev_id` (typically SSH username). Stores `channels` (JSONB list of notification endpoints) and `event_subscriptions`. |
| `notifications` | History of every notification sent: recipient, event_type, channels_attempted, channels_delivered, delivery_errors. |

### Embedding dimensions and indexing

Embeddings are `vector(1024)`. IVFFlat indexes are built by
`migrations/002_create_vector_indexes.sql` **after** bulk load — this yields
substantially better recall than indexing an empty table. Cosine distance
(`<=>`) is the default metric.

---

## 5. Data flow

### 5.1 Ingest path

Triggered by a filesystem watcher (`watchdog`) installed under launchd via
`com.devbrain.ingest.plist.template`. Entry point: `ingest/main.py`
(`scan` or `watch` mode). Pipeline lives in `ingest/pipeline.py`.

```
file created/modified
      │
      ▼
detect_adapter(path)  ────▶  one of 5 adapters claims it
      │                       (claude_code, codex, gemini,
      │                        openclaw, markdown_memory)
      ▼
sha256(file)  ─────── dedupe against raw_sessions.source_hash
      │
      ▼
adapter.parse() → UniversalSession (USF v1.0 dataclass)
      │
      ▼
insert_raw_session()  ───── stores full structured JSON as raw_content
      │                     + plain-text form for the embedder
      ▼
chunk_text(text, max_tokens=400, overlap=80)
      │
      ▼
embed_batch(chunks) via Ollama  ────▶  1024-d vectors
      │
      ▼
insert_chunk() per chunk with (source_type='session', source_id=raw_id,
                               source_line_start, source_line_end)
```

Summarization is a separate pass (`ingest/summarize.py`,
`ingest/backfill_summaries.py`) — it runs asynchronously so ingest latency
stays low.

**Adapters** are the only extension point on the ingest side. Each implements
`detect(path) -> bool` and `parse(path) -> UniversalSession | None`. The
`UniversalSession` format (`ingest/adapters/base.py`) is the single shape
every downstream consumer expects. Project attribution is either
path-based (match the file's path against `ingest.project_mappings`) or
content-based (scan agent_id, keywords, etc.).

### 5.2 Query path (via MCP)

An agent's view into DevBrain is always an MCP tool call. Example flow for
`deep_search`:

```
MCP client calls deep_search(query="auth pattern", depth="auto", limit=10)
      │
      ▼
mcp-server embeds the query via Ollama → 1024-d vector
      │
      ▼
SELECT ... ORDER BY c.embedding <=> $1::vector LIMIT $2
  (scoped to current project unless cross_project=true)
      │
      ▼
for each top result, if depth=auto and score > 0.6:
    fetch raw_sessions.raw_content
    slice ±25 lines around source_line_start/end
    attach as `full_context`
      │
      ▼
return JSON: results[] with chunk_id, content, score, source_ref, full_context
```

Every chunk carries enough metadata (`source_type`, `source_id`, line range)
that a follow-up `get_source_context(chunk_id, window_lines=50)` can pull
the raw transcript with an arbitrarily wide window. This **summary → drill
down** pattern is how DevBrain avoids the "vector search returns useless
fragments" problem.

### 5.3 Factory path

Triggered by the `factory_plan` MCP tool, which creates a `factory_jobs`
row and spawns `factory/run.py <job_id>` as a detached child process. The
orchestrator (`factory/orchestrator.py`) drives the state machine
(`factory/state_machine.py`):

```
queued
  │
  ▼
PLANNING ──────────▶ BLOCKED ◀──── file_locks conflict during
  │                    │            lock acquisition in PLANNING
  │                    │            or fix loop
  │                    ▼
  │              dev resolves:
  │                proceed │ replan │ cancel
  │                    │
  ▼                    ▼
IMPLEMENTING ◀─── (proceed / replan routes back here)
  │
  ▼
REVIEWING ────── findings with BLOCKING markers? ──┐
  │                                                ▼
  │                                            FIX_LOOP
  │                                                │
  ▼                                                │
QA ──────────── lint/test failures? ───────────────┤
  │                                                │
  ▼                                                │
READY_FOR_APPROVAL                                 │
  │                                                │
  │ dev calls factory_approve                      │
  │                                                │
  ▼                                                │
APPROVED ───▶ DEPLOYED                             │
                                                   │
        error_count ≥ max_retries ─────────────────┤
                                                   ▼
                                      cleanup_agent.attempt_recovery()
                                              │
                                         recovered? ─── yes ─▶ IMPLEMENTING
                                              │ no
                                              ▼
                                            FAILED
```

Key behaviors:

- **Each phase is a CLI subprocess.** `factory/cli_executor.py` spawns
  `claude`, `codex`, or `gemini` with a crafted prompt, captures stdout,
  and stores it as a `factory_artifacts` row. CLI assignment is per-phase
  and configurable (`factory.cli_preferences` in `devbrain.yaml`).
- **File locks are acquired after PLANNING.** The plan parser
  (`factory/plan_parser.py`) extracts affected file paths; the
  `FileRegistry` attempts all-or-nothing acquisition. On conflict, the job
  transitions to BLOCKED and emits a `lock_conflict` notification. A human
  unblocks via `devbrain_resolve_blocked(action=proceed|replan|cancel)`,
  which spawns a new factory process to execute the resolution.
- **Review detects BLOCKING findings** by regex. Any match routes to
  FIX_LOOP, which re-invokes the implementer with prior findings and the
  fix history for convergence context.
- **QA is programmatic, not LLM.** It runs the project's configured lint
  and test commands and stores output as an artifact.
- **Cleanup agent** (`factory/cleanup_agent.py`) has two jobs:
  - *On-error recovery*: when `error_count ≥ max_retries`, one focused
    diagnosis/fix attempt before FAILED.
  - *Post-run housekeeping*: runs after every terminal state — archives
    the job, summarizes artifacts, cleans up dead branches, stores a
    `factory_cleanup_reports` row.
- **Human approval is mandatory.** The factory never pushes or merges. It
  leaves a branch ready for review; `factory_approve` flips status but
  doesn't touch git remote.

The learning loop (`factory/learning.py`) extracts review findings from
completed jobs and surfaces them as "lessons" prepended to future planning
prompts — a poor man's feedback loop that keeps agents from repeating the
same class of mistakes.

---

## 6. Configuration

Precedence (highest to lowest):

1. **Environment variables** (`DEVBRAIN_*`) — always win.
2. **`config/devbrain.yaml`** — gitignored, copied from
   `config/devbrain.yaml.example`.
3. **Built-in defaults** in `factory/config.py` and `ingest/config.py`.

Key env vars:

| Var | Purpose |
|-----|---------|
| `DEVBRAIN_HOME` | Root of the repo (used by launchd template). |
| `DEVBRAIN_DB_HOST`, `DEVBRAIN_DB_PORT`, `DEVBRAIN_DB_USER`, `DEVBRAIN_DB_PASSWORD`, `DEVBRAIN_DB_NAME` | Postgres connection. |
| `DEVBRAIN_OLLAMA_URL` | Ollama base URL for embedding + summarization. |
| `DEVBRAIN_PROJECT` | Default project slug for MCP tool calls. |

The YAML mirrors these plus everything non-secret: adapter watch paths,
project mappings, chunk sizes, factory CLI preferences, cleanup timers,
notification channels.

### Instance pattern

A **DevBrain instance** is a thin wrapper repo that pulls DevBrain in as a
submodule and adds its own `instance.yaml` with project-specific rules —
compliance constraints, model preferences, extra notification channels.

v0.1 assumes **one DB namespace per host** (the `devbrain` schema is
shared). Multi-instance operational isolation (separate DBs or schemas per
instance) is Phase 1 work. The authoritative pattern doc is
[`docs/INSTANCE_PATTERN.md`](docs/INSTANCE_PATTERN.md) *(planned —
hardening plan task #7)*.

---

## 7. MCP tool surface

All 14 tools are defined in
[`mcp-server/src/index.ts`](mcp-server/src/index.ts).

**Memory — the core API:**

| Tool | Purpose |
|------|---------|
| `deep_search` | Vector search over chunks with auto drill-down to raw context. Accepts `source_types` filter and `cross_project` flag. |
| `store` | Write a `decision`, `pattern`, `issue`, or `note` + auto-embed a chunk twin. |
| `get_source_context` | Pull the raw transcript around a chunk_id with configurable window. |
| `list_projects` | Enumerate registered projects. |
| `get_project_context` | Aggregate snapshot: project metadata, recent decisions, open issues, relevant patterns, active factory jobs, active file locks. |

**Session lifecycle:**

| Tool | Purpose |
|------|---------|
| `end_session` | Store a session summary (decisions_made, files_changed, issues_found, next_steps) as an embedded chunk. |
| `startup` *(MCP prompt, not tool)* | Auto-injected context block reminding the agent to call `get_project_context` and `deep_search` before work. |

**Factory:**

| Tool | Purpose |
|------|---------|
| `factory_plan` | Submit a feature spec. Creates the job and spawns the orchestrator. |
| `factory_status` | Active jobs + per-job artifacts. |
| `factory_approve` | `approve` / `reject` / `request_changes` on a `ready_for_approval` job. |
| `factory_cleanup` | Archive a terminal job. |
| `factory_file_locks` | Who's holding which files right now (debugging + coordination). |
| `devbrain_resolve_blocked` | Resolve a BLOCKED job: `proceed` / `replan` / `cancel`. |

**Notifications:**

| Tool | Purpose |
|------|---------|
| `devbrain_notify` | Send an event to a registered dev through their configured channels. Called by agents mid-run. |

---

## 8. Extension points

### 8.1 Add an ingest adapter

1. Create `ingest/adapters/<app>.py` with a class implementing `detect(path)`
   and `parse(path) -> UniversalSession | None`. Follow the pattern in
   `claude_code.py` or `gemini.py`.
2. Instantiate it in `ingest/pipeline.py`'s `ADAPTERS` list.
3. Add config under `ingest.adapters.<app>` in
   `config/devbrain.yaml.example` — at minimum `enabled`, `watch_paths`,
   `file_pattern`, and your project detection strategy.

The universal session format is intentionally small. If your source has
features the base dataclass doesn't model (e.g. multi-modal messages),
encode them in `metadata` rather than expanding the base class.

### 8.2 Add a notification channel

1. Create `factory/notifications/channels/<name>.py` subclassing
   `NotificationChannel` from `factory/notifications/base.py`.
2. Register with `default_registry` at module import time.
3. Add it to the import list in `factory/notifications/router.py`.
4. Document config in `docs/notifications/<name>.md` and add an entry to
   `notifications.channels.<name>` in the config template.

Eight channels ship with v0.1: `tmux`, `smtp`, `gmail_dwd`, `gchat_dwd`,
`telegram_bot`, `webhook_slack`, `webhook_discord`, `webhook_generic`.

### 8.3 Instance-specific customization

Do not fork DevBrain. Create an instance repo:

```
myorg-brain/
├── devbrain/              # submodule → nooma-stack/devbrain
├── instance.yaml          # projects, compliance rules, channel prefs
├── prompts/               # optional: custom planning/review prompts
└── migrations-extra/      # optional: instance-only tables
```

Configuration is read from the instance's `instance.yaml` via the same
loader as `config/devbrain.yaml`. See `docs/INSTANCE_PATTERN.md` *(planned
— hardening plan task #7)* for the full contract and an
[example instance](examples/instance-example/) *(planned — hardening plan
task #8)*.

---

## 9. What's intentionally NOT in v0.1

DevBrain's long-term roadmap has 8 phases. v0.1 is **Phase 0: Hardening** —
get to the point where a stranger can clone the repo and run it.
See [`docs/plans/2026-04-15-hardening-plan.md`](docs/plans/2026-04-15-hardening-plan.md)
for the full Phase 0 scope.

Explicitly deferred:

- **Memory model refactor (Phase 2).** Today's `decisions` / `patterns` /
  `issues` split will collapse into a unified `memory` table with richer
  typing. No schema change in v0.1. *(Shipped — see
  [`docs/MEMORY_MODEL.md`](docs/MEMORY_MODEL.md).)*
- **Discipline layer (Phase 3).** Curator agent, eval agents, and a rule
  engine that decides what's worth remembering and when to supersede old
  memories. v0.1 stores everything the agent asks to store.
  Phase 3 also adds three integrity properties borrowed from
  [Atlas](https://github.com/RichSchefren/atlas) (concepts, not code):
  dependency-cascade re-evaluation when a memory is superseded
  (Ripple-style), a hash-chained append-only audit ledger for HIPAA-grade
  tamper detection, and a postulate-based test suite that formally
  asserts the discipline layer's invariants. Full design in
  [`docs/plans/2026-04-29-phase-3-discipline-layer.md`](docs/plans/2026-04-29-phase-3-discipline-layer.md).
- **Graph layer (Phase 5).** Apache AGE integration and a `memory_edges`
  table for relationship-aware retrieval.
- **Cognify / Memify pipeline split (Phase 6).** Separation of raw ingest
  (memify) from knowledge extraction (cognify). v0.1 does both inline.
- **Multi-instance operational isolation.** Today all instances share one
  `devbrain` schema on one host. Separate DBs / schemas per instance is
  Phase 1 feedback from the first real instance deployment.
- **Cross-platform full support.** macOS is primary. Linux should work
  with notes. Windows is out of scope for v0.1.
- **Retry logic / Ollama fallback / credentials encryption.** Operational
  polish, deferred.

---

## 10. Glossary

**Adapter** — A pluggable parser for one source app's transcript format
(Claude Code `.jsonl`, Gemini `session-*.json`, OpenClaw, Markdown memory).
Produces a `UniversalSession`.

**Agent** — An AI coding tool (claude, codex, gemini, or a custom MCP
client) that talks to DevBrain through the MCP server.

**Channel** — A delivery mechanism for notifications (tmux, email, chat,
webhook). Devs choose which channels to subscribe to.

**Chunk** — A small (~400 token) text segment with a 1024-d embedding.
The unit of vector search. Every chunk points back to a raw source it was
derived from.

**Decision** — A structured architecture or design choice stored with
context, rationale, and alternatives. Supersession-aware.

**Factory job** — A work item submitted via `factory_plan`. Moves through
the state machine from `queued` to `approved`/`deployed`/`failed`, leaving
artifacts at each phase.

**Instance** — A thin wrapper repo that consumes DevBrain as a submodule
and adds org-specific config, projects, and rules. v0.1 supports one DB
namespace per host.

**Issue** — A bug fix worth remembering: root cause, applied fix, how to
prevent recurrence.

**Pattern** — A reusable code or architecture approach with a description,
example code, and tags.

**Raw session** — A losslessly stored transcript, identified by
`(source_app, source_hash)`. The ground truth every chunk drills back to.

**Session** — One conversation between a user and an agent, as emitted by
a source app and ingested by an adapter.

**USF (Universal Session Format)** — The normalized dataclass shape every
adapter produces (`ingest/adapters/base.py`). Version 1.0.
