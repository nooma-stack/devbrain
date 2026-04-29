# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/).

## [Unreleased] — Phase 3 / Atlas integrations

### Added
- **Migration 014** — `devbrain.memory_dependencies` typed-edge table (`cites` / `depends_on` / `supersedes` / `contradicts`) with backfill from legacy `decisions.superseded_by` chain. Atlas Step 1 of the Phase 3 design ([docs/plans/2026-04-29-phase-3-discipline-layer.md](docs/plans/2026-04-29-phase-3-discipline-layer.md), PR #67).
- **MCP `store` tool** — accepts `depends_on` and `supersedes` UUID arrays. Resolves either `memory.id` or legacy `decisions/patterns/issues.id` (best-effort lookup) and inserts edges into `memory_dependencies`. Idempotent via the unique `(from_memory_id, to_memory_id, edge_type)` constraint.
- **`memory.ts` helpers** — `recordMemory` now returns the `memory.id` for downstream edge wiring (was `void`); new `resolveMemoryId(uuid)` and `recordMemoryDependency(...)` helpers.
- **Migration 015** — `devbrain.memory_ledger` hash-chained append-only audit table (SHA-256 row chain) + `_memory_ledger_record()` AFTER trigger on `devbrain.memory` (insert/update/delete) + `verify_chain(start_seq, end_seq)` SQL function returning the first chain divergence. Atlas Step 2. Tamper detection only — payload contents are NOT duplicated, only hashed.
- **`pgcrypto` extension** added (was previously unused in the schema).
- **`devbrain audit verify`** CLI command — wraps `verify_chain()` with project scoping, JSON output, and exit codes (0 intact / 1 broken / 2 operational failure) suitable for cron and CI smoke tests. Atlas Step 3.

## [Unreleased] — Factory Hardening Sprint

Eleven PRs landed 2026-04-23 → 2026-04-25 hardening the dev-factory pipeline: per-job worktrees, reviewer calibration, ff-only sync, structured JSON findings, schema-migration plumbing, and install-time identity registration.

### Added
- PR #28 — feat(factory): per-job git worktrees (foundation for parallel execution) (63dcee5)
- PR #29 — feat(factory): fix-loop triggers on WARNING findings when configured (3c87fb6)
- PR #30 — feat(factory): calibrate reviewer prompts with severity-cost guidance (2521533)
- PR #31 — feat(factory): WARNING oscillation guardrail — escalate when findings persist (2c) (2beb74a)
- PR #34 — feat(factory): structured JSON findings block for reviewers (task #16) (f05c5a2)
- PR #35 — feat(factory): auto-pull factory-host to origin at readiness entry (task #17) (9889591)
- PR #37 — feat(factory): migration applier — schema_migrations + devbrain migrate + install hook (task #19) (975fff0)
- PR #38 — feat(install): auto-register default dev + INSTALL.md cross-refs (task #20) (e1a65d5)

### Fixed
- PR #32 — fix(factory): count stacked severity prefixes like **1. WARNING (d2593c9)
- PR #33 — feat(factory): ff-only sync before push in factory_approve (task #13) (4a866da)
- PR #36 — feat(factory): harden JSON findings parser against multi-block attacks (task #18) (b44f2d2)
