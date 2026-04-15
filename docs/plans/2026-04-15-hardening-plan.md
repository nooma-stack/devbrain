# DevBrain Standalone Hardening Plan

> **Historical planning document** for the Phase 0 hardening milestone.
> Remaining references to specific projects/paths in this doc are part
> of the audit checklist that drove the hardening work — they describe
> what was searched for and removed. See [ARCHITECTURE.md](../../ARCHITECTURE.md)
> and [INSTALL.md](../../INSTALL.md) for the canonical current state.

**Date**: 2026-04-15
**Phase**: 0 (of 0-8 roadmap)
**Goal**: Make DevBrain installable by a stranger from the GitHub repo alone, with no external context or tribal knowledge required.

---

## Success criteria

A fresh machine can:

1. Clone `nooma-stack/devbrain`
2. Follow only what's in the repo (README → INSTALL → docs)
3. End with DevBrain running: Postgres up, Ollama models pulled, MCP server built, ingest service active
4. `devbrain doctor` exits 0
5. MCP server responds to `list_projects` tool call
6. A test session gets ingested end-to-end

The test of success: **clean install on the Mac Studio using only the hardened repo.** That install is Phase 1.

---

## Non-goals for Phase 0

Explicit exclusions to keep scope controlled:

- **Memory model refactor** (Phase 2) — no schema changes for unified `memory` table
- **Discipline layer** (Phase 3) — no curator/eval agents/rule engine
- **Graph layer** (Phase 5) — no Apache AGE, no `memory_edges`
- **Cognify/Memify pipelines** (Phase 6) — ingest stays as-is
- **Multi-instance operational isolation** — documented as a pattern, not implemented. The first instance's concerns (DB namespacing, multiple launchd services) are Phase 1 feedback.
- **Cross-platform full support** — macOS is primary. Linux "should work" with notes. Windows explicitly not supported in v0.1.
- **Retry logic / Ollama fallback** — operational polish, later.
- **Encryption at rest for credentials** — security hardening, later.
- **Automatic migration version tracking** — manual ordering remains assumed.

---

## Priority-ordered tasks

Derived from the Top 10 gaps identified in the 2026-04-13 audit. Ordered by dependency — earlier tasks unblock later ones.

### 1. Fix hardcoded paths

**Why first**: blocks clean install entirely. Without this, nothing else matters.

**Files to update:**
- `com.devbrain.ingest.plist` — remove `/Users/patrickkelly/devbrain/ingest/.venv/bin/python`. Replace with a bootstrap shell that resolves paths via `$DEVBRAIN_HOME` env var.
- `factory/run.py` (line 36) — remove hardcoded `DATABASE_URL`. Read from env / config.
- `factory/cli.py` (lines ~18-19) — remove hardcoded `DATABASE_URL`, `OLLAMA_URL`. Read from env / config.
- `ingest/main.py` (lines ~30-31) — remove hardcoded `WATCH_DIRS`. Read from config.
- Search repo for any remaining `/Users/patrickkelly`, `~/Developer/lighthouse`, `brightbot` references. Remove or generalize.

**Acceptance**: grep `'/Users/'` in repo returns no matches outside documentation/examples.

### 2. Externalize config via env vars

**Why second**: Task #1 exposes values that need a home. This defines where.

**Deliverables:**
- `.env.example` at repo root with every env var documented (comments explain purpose, default, required-or-optional)
- Config precedence: env > `config/devbrain.yaml` > hardcoded defaults. Document in `INSTALL.md`.
- Load environment before config.yaml in all entrypoints (MCP server, factory CLI, ingest pipeline, `bin/devbrain`)

**Env vars in scope:**
```
DEVBRAIN_HOME=/path/to/devbrain              # Required; replaces hardcoded paths
DEVBRAIN_DATABASE_URL=postgresql://...       # Required
DEVBRAIN_OLLAMA_URL=http://localhost:11434   # Default; required if remote
DEVBRAIN_EMBEDDING_MODEL=snowflake-arctic-embed2
DEVBRAIN_SUMMARY_MODEL=qwen2.5:7b
DEVBRAIN_MCP_PORT=3901
DEVBRAIN_LOG_LEVEL=info
```

**Acceptance**: with `.env` populated and `config/devbrain.yaml` empty, DevBrain starts and runs.

### 3. Add `devbrain doctor` command

**Why third**: gives us a single-command verification tool for every later task and for install validation.

**Scope** — extend existing `devbrain status`:
- DB reachable (psql connect + pgvector extension present)
- Ollama reachable (`GET /api/tags`)
- Embedding model available (`DEVBRAIN_EMBEDDING_MODEL` is in pulled models list)
- Summary model available (`DEVBRAIN_SUMMARY_MODEL` is in pulled models list)
- MCP server built (`mcp-server/dist/` exists)
- Ingest venv exists (`ingest/.venv/bin/python` exists)
- Config file valid (parses without error)
- All required env vars set

Exit 0 on full pass. Exit 1 with structured report on any failure.

**Acceptance**: `devbrain doctor` runs and gives clear pass/fail for each check.

### 4. Write INSTALL.md

**Why fourth**: tasks 1-3 produce the actual install path; now we document it.

**Sections:**
- Prerequisites (Docker Desktop, Python 3.11+, Node 20+, Ollama, git, gh)
- Step-by-step:
  1. Clone repo, `cd` in
  2. Copy `.env.example` to `.env`, fill in values
  3. `docker compose up -d devbrain-db`
  4. `ollama pull snowflake-arctic-embed2 && ollama pull qwen2.5:7b`
  5. `cd mcp-server && npm install && npm run build`
  6. `cd ingest && python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
  7. Copy `config/devbrain.yaml.example` to `config/devbrain.yaml`
  8. Install launchd service: `./scripts/install-ingest-service.sh`
  9. `./bin/devbrain doctor` — verify all pass
  10. `./bin/devbrain register --dev-id <yours>` — register your dev identity
- Platform notes: macOS primary, Linux with cron+systemd notes, Windows not supported
- Troubleshooting section (common failures from `devbrain doctor`)

**Acceptance**: an AI agent or new dev following INSTALL.md gets to a working DevBrain install without reading other docs.

### 5. Write ARCHITECTURE.md

**Why fifth**: independent of install. Explains what DevBrain *is* for strangers arriving at the repo.

**Sections:**
- Overview: what DevBrain is, who it's for, what problem it solves
- Component diagram (text-based ASCII): MCP server ↔ Postgres/pgvector ↔ Ingest pipeline ↔ Ollama ↔ Factory orchestrator ↔ Notifications
- Data flow: ingest path (watch → parse → chunk → embed → store), query path (MCP tool → vector search → drill-down), factory path (plan → implement → review → qa → approve)
- Responsibilities: one paragraph per component
- Forward-looking note: link to `docs/roadmap/` for Phases 2-8 vision (memory/knowledge/discipline unification). Make clear this is v0.1 baseline.
- Explicit non-goals of DevBrain as a product

**Must not**: reference BrightBot repo, reference external design doc, assume reader knows anything beyond "agents need persistent memory."

**Acceptance**: a stranger can read ARCHITECTURE.md and understand what DevBrain does and how its pieces fit, without needing other context.

### 6. Write README.md

**Why sixth**: needs INSTALL.md and ARCHITECTURE.md to link to.

**Sections:**
- 2-sentence pitch ("DevBrain is a local-first persistent memory and dev factory system for coding agents…")
- Why it exists (vendor-lock-in story, model-agnostic stance)
- Quick start (5 lines → link to INSTALL.md for detail)
- Architecture at a glance (1 paragraph → link to ARCHITECTURE.md)
- What's included (MCP server, ingest, factory, notifications — brief list)
- Supported agents (Claude Code, OpenClaw, Codex, Gemini)
- Status badge, license, contributing link
- Link to roadmap

Keep under 300 lines. README is the front door, not the manual.

**Acceptance**: landing on the GitHub repo, a stranger knows within 30 seconds what DevBrain is and whether it's relevant to them.

### 7. Write INSTANCE_PATTERN.md

**Why seventh**: documents the submodule + YAML pattern so the first instance (Phase 1) has a documented home to slot into. Does not require implementation — docs only for Phase 0.

**Sections:**
- What an instance is (org-specific example: an org instance carries compliance context like HIPAA/FERPA/SOC2)
- Repository structure: `instance-name/` with DevBrain as submodule + `instance.yaml` config
- Minimum `instance.yaml` fields (projects, compliance rules, notification prefs, model preferences)
- How to bootstrap a new instance
- Known limitations for v0.1: single DB namespace assumed (multi-instance operational isolation is Phase 1 feedback work)
- Example instance at `examples/instance-example/`

**Acceptance**: a stranger could start a new instance of DevBrain for their own org following this doc.

### 8. Add example instance

**Why eighth**: concrete reference for INSTANCE_PATTERN.md.

**Deliverables:**
- `examples/instance-example/instance.yaml` — minimal viable config
- `examples/instance-example/README.md` — walkthrough of what each field does
- `examples/instance-example/.gitkeep` for empty dirs where needed

**Acceptance**: copy-able template, not just a docs artifact.

### 9. Add LICENSE

User's call: Apache 2.0 or MIT. Recommend **Apache 2.0** for nooma-stack projects (patent grant, compatible with downstream instance forking, matches Cognee/Enso/MemPalace ecosystem norms).

**Acceptance**: file exists, SPDX identifier in repo metadata.

### 10. Add CONTRIBUTING.md

Brief — this is an early-stage project. Sections:
- How to report issues
- PR format (conventional commits)
- Local dev setup (reference INSTALL.md)
- Code style (black for Python, Prettier for TS)
- Sign-off required (DCO)

Keep under 100 lines. Can expand later.

**Acceptance**: file exists and is linked from README.

### 11. Remove external references (final pass)

Cleanup pass across the whole repo:
- Grep for `BrightBot`, `brightbot`, `~/Developer/lighthouse`, `patrick@lighthouse-therapy.com`
- Replace with generic language or remove
- Check docstrings, comments, config examples, ingest adapter doctests

**Acceptance**: repo contains no Lighthouse-specific or Patrick-specific content outside of attributions.

### 12. Dry-run verification

Before tagging v0.1.0:

**Setup:**
- Fresh macOS user account (or VM) — no DevBrain history
- Only the public GitHub repo accessible
- Notes doc open to capture every place docs fell short

**Procedure:**
- Follow README → INSTALL.md to set up
- Run `devbrain doctor` until green
- Use MCP `list_projects` via Claude Code
- Ingest a test markdown memory file, verify it appears in search

**Gaps go back into the repo as fixes.**

### 13. Tag v0.1.0

After dry-run passes cleanly without any undocumented workarounds:

```bash
git tag -a v0.1.0 -m "Standalone-ready release"
git push --tags
```

Mark GitHub release with changelog and install instructions.

---

## Execution order

Many tasks are parallelizable in principle, but dependency order matters:

```
1. Fix hardcoded paths
2. Externalize config via env vars
      ↓
3. devbrain doctor                  ← depends on 1, 2
      ↓
4. INSTALL.md                       ← depends on 1, 2, 3

5. ARCHITECTURE.md                  ← independent, can run in parallel
7. INSTANCE_PATTERN.md              ← independent
8. examples/instance-example/       ← depends on 7
9. LICENSE                          ← trivial
10. CONTRIBUTING.md                 ← trivial

      ↓ (after 4 and 5)
6. README.md                        ← needs INSTALL, ARCHITECTURE to link to

      ↓ (after all above)
11. Remove external references       ← final sweep
12. Dry-run verification             ← gate for release
13. Tag v0.1.0
```

**Recommended sessions:**
- Session A (focused code work): Tasks 1, 2, 3
- Session B (doc-writing sprint, parallelizable): Tasks 4, 5, 7, 8, 9, 10 — can be dispatched as parallel sub-agents
- Session C (synthesis + final): Tasks 6, 11
- Session D (validation): Tasks 12, 13 — this is Phase 1 overlap on the Mac Studio

---

## Risks and watchpoints

- **`ingest/requirements.txt` audit**: audit flagged only 3 deps but real imports suggest `psycopg2`, `watchdog`, `pyyaml`, `openai` or similar. Verify on Task 4.
- **Migration version tracking**: no table tracks applied migrations currently. For v0.1 we rely on manual ordering, but **add a note in INSTALL.md that fresh installs run all migrations in order**.
- **launchd plist portability**: even with env-var paths, the plist is macOS-only. INSTALL.md Linux section should document a systemd alternative or a "run in foreground" fallback.
- **Ollama model size warning**: `snowflake-arctic-embed2` and `qwen2.5:7b` are several GB each. INSTALL.md should say "expect ~10GB download" up front so users aren't surprised.
- **Docker Desktop license**: on macOS with commercial use, Docker Desktop requires a license for orgs >250 employees / >$10M revenue. INSTALL.md should note Colima or OrbStack as alternatives.

---

## What this plan intentionally defers

Everything beyond "can a stranger install this." The Phase 2+ architecture (memory model, discipline, graph, cognify/memify) is explicitly out of scope. The whole point of Phase 0 is to lock down a stable base so those larger refactors can proceed on firm ground.

If gaps surface during dry-run that require more than documentation (e.g., the instance pattern fundamentally doesn't work without DB namespacing), those become **Phase 1 tasks**, not Phase 0 scope creep.

---

## Definition of done for Phase 0

- [ ] All 13 tasks above complete
- [ ] Dry-run passes on a fresh environment without undocumented workarounds
- [ ] `devbrain doctor` exits 0 after INSTALL.md walkthrough
- [ ] Zero `/Users/patrickkelly` or BrightBot/Lighthouse references outside attributions
- [ ] v0.1.0 tagged and released on GitHub
- [ ] README reviewed by a stranger (or stand-in stranger test) who could describe what DevBrain is in one paragraph

When all of these pass, Phase 0 is done and Phase 1 (clean install on a fresh machine + first instance bootstrap) can begin.
