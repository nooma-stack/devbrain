# DevBrain Memory Model

> Phase 2 reference. Source of truth for the unified `devbrain.memory` table.

## What this replaces

`devbrain.memory` consolidates four legacy tables — `chunks`, `decisions`,
`patterns`, `issues` — under a single schema. One table beats four because
strength/lifecycle, embedding search, graph edges (Phase 5), and the
discipline pipeline (Phase 3) all want to operate over a single substrate;
maintaining four parallel surfaces with the same lifecycle hooks would
duplicate every retrieval, decay, and promotion query.

## The `kind` column — five values

- `chunk` — searchable transcript segment from raw_sessions ingest
- `decision` — architecture/design choice (replaces `devbrain.decisions`)
- `pattern` — reusable approach (replaces `devbrain.patterns`)
- `issue` — bug/lesson with root cause + prevention (replaces `devbrain.issues`)
- `session_summary` — end_session summary captured by the MCP server

Choose the kind that best describes the artifact at write time; the curator
may reclassify in P3.

## Lifecycle (Phase 6)

`strength` follows the Enso-derived formula
`strength = decay × retrievalBoost × emotionalMultiplier × usageFeedback`,
half-life 7-60d. Thresholds: `< 0.1` → `archived_at = now()`; `0.1-0.3` →
flagged for review; `> 0.7` → stable. The formula lives in the Phase 6
memify worker (not in this PR — this PR only stores the `strength`,
`hit_count`, `last_hit` columns at default values).

## Discipline tiers (Phase 3 prep)

- `memory` — default. Surfaces in `deep_search` results.
- `lesson` — promoted by the curator agent based on observed effectiveness.
  Gets injected into the curator brief.
- `rule` — compliance-grade. Feeds the rule engine.

Promotion path: `memory` → `lesson` → `rule` (demotion is allowed). All
three tiers live in the same table; `tier` is the discipline marker.

Phase 3 will add two companion tables — `memory_dependencies` (typed edges
for cascade re-evaluation on supersession) and `memory_ledger` (hash-chained
audit trail for HIPAA contexts) — plus a `tests/postulates/` directory of
formal invariants. See
[`plans/2026-04-29-phase-3-discipline-layer.md`](plans/2026-04-29-phase-3-discipline-layer.md)
for the full design and implementation order.

## Status

This PR (P2.a) adds the table only. Adapters land in **P2.b**, historical
data backfill in **P2.c**, legacy table drops in **P2.d**. Until then,
`devbrain.memory` is empty and reads should still go to the legacy tables.
