# DevBrain

**Local-first persistent memory and dev factory for coding agents.**

DevBrain gives your coding agents a memory that survives across sessions,
across tools, and across machines — without shipping your code or your
conversations to a third party. It also provides an opinionated dev factory
pipeline: structured plan → implement → review → QA → approve loops with
human-in-the-loop gates.

> **Status:** v0.1 — early. Runs end-to-end on macOS. Linux supported with
> caveats. See [INSTALL.md](INSTALL.md) for setup and platform notes.

---

## Why DevBrain exists

Coding agents forget. Every Claude Code, Codex, Gemini, or OpenClaw session
starts from scratch — no recall of prior decisions, no awareness of past
bugs, no understanding of how pieces of a codebase connect. Organizations
paper over this with hand-crafted CLAUDE.md files, tribal knowledge, and
copy-pasted context. It breaks down at scale.

DevBrain solves this as shared infrastructure:

- **Lossless session capture** across Claude Code, OpenClaw, Codex, Gemini,
  and plain Markdown memory files.
- **Semantic + structured memory** in one place — full raw transcripts plus
  vector-searchable chunks plus explicit decisions, patterns, and issues.
- **Cross-tool access** via MCP, so any MCP-compatible agent can query the
  same memory.
- **Dev factory pipeline** with multi-phase orchestration, file locking for
  multi-dev coordination, and a self-healing cleanup agent.
- **Model-agnostic**: you bring your own Ollama models, your own Claude/
  Codex/Gemini subscriptions. DevBrain doesn't call paid APIs on your behalf.
- **Postgres-only backend** with pgvector. One database, one backup story.

---

## Quick start

```bash
git clone <repo-url> devbrain && cd devbrain
cp .env.example .env
cp config/devbrain.yaml.example config/devbrain.yaml
docker compose up -d devbrain-db
ollama pull snowflake-arctic-embed2 && ollama pull qwen2.5:7b
(cd mcp-server && npm install && npm run build)
(cd ingest && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./bin/devbrain doctor
```

If `devbrain doctor` exits 0, you're running. See
[**INSTALL.md**](INSTALL.md) for the detailed walkthrough, prerequisites,
platform notes, and troubleshooting.

---

## Architecture at a glance

```
 ┌─────────────────────┐    ┌──────────────────────┐
 │  Your AI agents     │◀──▶│   MCP Server (TS)    │
 │  (Claude Code,      │MCP │   14 tools           │
 │   Codex, Gemini,    │    └──────────┬───────────┘
 │   OpenClaw, …)      │               │
 └─────────────────────┘       ┌───────┴────────────────────┐
         │                     │  PostgreSQL 17 + pgvector  │
         │ writes sessions     │  projects, sessions,       │
         ▼                     │  chunks, decisions,        │
 ┌─────────────────────┐       │  patterns, issues,         │
 │  Ingest pipeline    │──────▶│  factory jobs, locks,      │
 │  (Python, launchd)  │       │  notifications             │
 │  5 adapters         │       └───────┬────────────────────┘
 └─────────────────────┘               │
                                       ▼
                              ┌──────────────────────┐
                              │  Factory orchestrator│
                              │  (Python, subprocess │
                              │   spawns claude/     │
                              │   codex/gemini CLIs) │
                              └──────────┬───────────┘
                                         │
                                         ▼
                              ┌──────────────────────┐
                              │  Notifications       │
                              │  8 channels          │
                              └──────────────────────┘
```

External dependencies: Ollama (embedding + summarization, runs natively),
your chosen AI CLIs (Claude Code / Codex / Gemini — each under its own
subscription).

Read [**ARCHITECTURE.md**](ARCHITECTURE.md) for the component-by-component
breakdown, data model, and extension points.

---

## What's included in v0.1

- **MCP server** exposing 14 tools for memory search/store, session
  lifecycle, factory orchestration, notifications, and file-lock coordination.
- **Ingest pipeline** with 5 adapters (Claude Code, OpenClaw, Codex, Gemini,
  Markdown memory files) and a codebase indexer.
- **Factory pipeline** with 12-state state machine, cleanup/recovery agent,
  multi-dev file locking, blocked-state resolution, and a TUI dashboard.
- **Notification router** with 8 channels (tmux, SMTP, Gmail DWD, Google
  Chat DWD, Telegram, generic/Slack/Discord webhooks).
- **`devbrain doctor`** command — single-command install verification.
- **Instance pattern** documented (see [docs/INSTANCE_PATTERN.md](docs/INSTANCE_PATTERN.md))
  for running org-specific DevBrain instances without forking the engine.

## What's not in v0.1 (planned)

Larger architectural moves are deferred to later phases to keep v0.1 stable:

- **Phase 2:** Unified `memory` base model (collapse chunks / decisions /
  patterns / issues into one entity with strength + decay metadata).
- **Phase 3:** Three-stage discipline pipeline (Curator → Eval agents →
  compliance rule engine) with lesson extraction and graduation.
- **Phase 5:** Apache AGE graph layer for multi-hop retrieval and
  relationship-aware memory.
- **Phase 6:** Cognify / Memify pipeline split for continuous reweighting
  and self-improvement.

See [`docs/plans/2026-04-15-hardening-plan.md`](docs/plans/2026-04-15-hardening-plan.md)
for Phase 0 scope and the roadmap for what comes next.

---

## Supported agents

Out of the box, DevBrain's ingest adapters recognize sessions from:

- **Claude Code** (`~/.claude/projects/**/*.jsonl`)
- **OpenClaw** (`~/.openclaw/agents/*/sessions/**/*.jsonl`)
- **Codex CLI** (`~/.codex/sessions/**/*.jsonl`)
- **Gemini CLI** (`~/.gemini/tmp/**/session-*.json`)
- **Markdown memory files** (configurable paths)

Any MCP-compatible agent can read from DevBrain via the MCP server.

---

## Project structure

```
devbrain/
├── bin/devbrain                ← CLI entrypoint
├── factory/                    ← orchestrator, state machine, cleanup, notifications
├── ingest/                     ← watch + parse + chunk + embed pipeline
├── mcp-server/                 ← TypeScript MCP server (14 tools)
├── migrations/                 ← PostgreSQL schema
├── config/                     ← devbrain.yaml (gitignored) + example
├── scripts/                    ← installers and helpers
├── docs/                       ← INSTANCE_PATTERN, plans, guides
├── examples/instance-example/  ← template for creating your own instance
├── hooks/                      ← session-start hook for agents
├── ARCHITECTURE.md             ← how the pieces fit
├── INSTALL.md                  ← detailed install guide
└── README.md                   ← you are here
```

---

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Issues and PRs welcome — this is
early work, and the areas most in need of real-world feedback are the
install path, instance pattern, and the factory pipeline.

## Links

- **Roadmap:** [`docs/plans/2026-04-15-hardening-plan.md`](docs/plans/2026-04-15-hardening-plan.md)
- **Architecture:** [`ARCHITECTURE.md`](ARCHITECTURE.md)
- **Install:** [`INSTALL.md`](INSTALL.md)
- **Instance pattern:** [`docs/INSTANCE_PATTERN.md`](docs/INSTANCE_PATTERN.md)
