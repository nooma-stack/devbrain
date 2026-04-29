# Phase 3 — Discipline Layer (Curator + Eval + Compliance)

> **Status:** Design. No code in this PR — this doc captures the locked-in
> design decisions for Phase 3 plus three new refinements borrowed from
> [Atlas](https://github.com/RichSchefren/atlas) (cognitive memory system).
> Implementation lands in follow-up PRs.
>
> **Depends on:** Phase 2 (unified `devbrain.memory` table) — shipped in #42-#48.
> **Builds on locked decisions** from 2026-04-15 (see DevBrain `decisions` for
> "Locked design decisions for DevBrain v2 architecture", "Implementer
> influence pattern: coached before, directed after", "Prompt caching + shared
> context strategy for agent pipeline").

---

## 1. Goal

Turn DevBrain's memory from a passive store into a **disciplined, self-correcting
substrate**. Three loops to add on top of Phase 2's unified `memory` table:

- **Curator loop** — pre-emptively rank/promote/demote memories per job.
  Already partly described in prior decisions.
- **Eval loop** — domain-specific agents that detect violations
  post-implementation. Lesson graduation feeds new evals.
- **Compliance loop** — regulatory-grade rules (HIPAA in BrightBrain) with
  formal verification of belief-revision behavior.

Phase 3 also adds three **integrity properties** (this doc's main new content):

1. **Belief-revision via dependency cascade** — when a memory is invalidated,
   the system surfaces what depended on it for re-evaluation.
2. **Hash-chained audit ledger** — append-only, tamper-detectable record of
   every memory write. Enables HIPAA-grade audit trails.
3. **Postulate-based compliance test suite** — formal verification that the
   discipline layer's behaviors satisfy stated invariants.

---

## 2. Why these three (Atlas borrows)

[Atlas](https://github.com/RichSchefren/atlas) (RichSchefren/atlas, MIT) is an
open-source AGM-compliant cognitive memory system aimed at business decision
memory. Different domain than DevBrain's dev-workflow memory — but three of
its design ideas are genuinely valuable and orthogonal to its specific stack
(Neo4j, Obsidian, Limitless ingestion). We borrow the **concepts**, not the
code.

### 2.1 Belief revision (Atlas's "Ripple Engine")

Atlas's distinctive contribution: when a fact changes, every belief that
depended on the old fact is automatically marked suspect and re-evaluated by
walking `Depends_On` edges in the graph. Implements
[AGM postulates K\*2–K\*6](https://en.wikipedia.org/wiki/Belief_revision) plus
Hansson Relevance and Core-Retainment.

**Why DevBrain needs this:** today, when a `decision` is superseded or a
`pattern` is invalidated (security advisory on a library, framework
deprecation, internal API rename), nothing flags the *downstream* memories
that referenced it. The curator strength formula handles slow decay, not
cascading invalidation.

**Concrete dev examples:**

- A pattern row says "use `aiopg` for async Postgres." A later decision
  retracts it ("we standardized on `asyncpg`"). Today: both rows coexist,
  both surface in `deep_search`, the curator might prefer the newer one by
  recency but the older one keeps showing up. With cascade: marking the
  pattern superseded triggers the curator to re-rank or auto-archive any
  lesson/issue that cited it.
- A decision says "use HS256 JWTs." A security review supersedes it with
  "use ES256 JWTs." Cascade walks dependent issues ("HS256 token rotation
  procedure") and flags them for re-evaluation.

### 2.2 Hash-chained audit ledger

Atlas writes every belief revision into a SHA-256-chained SQLite ledger so
the history is tamper-detectable. Each row stores a hash of the previous row
plus the current write, forming a chain that breaks if any past row is
modified.

**Why DevBrain needs this:** HIPAA contexts (BrightBrain instance) require
auditable trails of who-knew-what-when. Today, a malicious or careless
actor with database access could `UPDATE devbrain.memory` and DevBrain
wouldn't notice. A parallel `memory_ledger` table with hash chaining
detects retroactive tampering.

This is **append-only metadata about writes**, not a duplicate store of the
content. Cheap. Verified by a `verify_chain()` SQL function that walks the
chain and reports the first break.

### 2.3 Postulate-based compliance testing

Atlas tests its belief-revision behavior against AGM postulates with 49 named
scenarios, run via pytest (`pytest tests/integration/test_agm_compliance.py`).
The **methodology** is the borrow: state your invariants formally, then write
parameterized scenarios that prove the system upholds them.

**Why DevBrain needs this:** Phase 3's "compliance rule engine" is currently
a black box in the roadmap. Borrowing Atlas's discipline gives us a concrete
shape:

```
test_postulates/
├── test_p1_supersession_invalidates_dependents.py
├── test_p2_archived_memory_excluded_from_curator_brief.py
├── test_p3_lesson_graduation_requires_n_effective_hits.py
├── test_p4_hipaa_phi_never_appears_in_cross_project_search.py
└── ...
```

Each postulate is a single sentence; each scenario is a deterministic
fixture that asserts the postulate holds. Same pattern as TDD, but applied
to memory-layer invariants instead of feature behavior.

---

## 3. Data model additions

All additions land in the `devbrain` schema as new migrations.

### 3.1 Dependency edges — `migrations/00X_memory_dependencies.sql`

```sql
CREATE TABLE devbrain.memory_dependencies (
    id              BIGSERIAL PRIMARY KEY,
    from_memory_id  UUID NOT NULL REFERENCES devbrain.memory(id) ON DELETE CASCADE,
    to_memory_id    UUID NOT NULL REFERENCES devbrain.memory(id) ON DELETE CASCADE,
    edge_type       TEXT NOT NULL,
        -- 'cites'        — narrative reference, weakest signal
        -- 'depends_on'   — invalidating to_memory should re-evaluate from_memory
        -- 'supersedes'   — from_memory replaces to_memory (terminal)
        -- 'contradicts'  — surfaced as an integrity issue
    confidence      REAL NOT NULL DEFAULT 1.0,  -- 0.0 — 1.0
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by      TEXT,                       -- 'curator' / 'agent' / 'manual'
    metadata        JSONB,
    UNIQUE (from_memory_id, to_memory_id, edge_type)
);

CREATE INDEX ON devbrain.memory_dependencies (from_memory_id);
CREATE INDEX ON devbrain.memory_dependencies (to_memory_id);
CREATE INDEX ON devbrain.memory_dependencies (edge_type);
```

**Edges land via three sources:**

1. **Explicit** — `store(type='decision', supersedes=<uuid>)` or `store(type='pattern',
   depends_on=[<uuid>, <uuid>])` parameter.
2. **Curator agent** — during the curator pass, parses memory bodies for
   citations of other memories' titles/IDs. Adds `cites` edges with confidence
   < 1.
3. **Phase 5 graph backfill** — when Apache AGE lands, treat the edge table
   as the source of truth for graph traversals.

**Cascade re-evaluation on supersession:**

```sql
-- pseudocode in curator agent
UPDATE devbrain.memory SET archived_at = now() WHERE id = $superseded_id;

-- find dependents
SELECT m.* FROM devbrain.memory m
JOIN devbrain.memory_dependencies e ON e.from_memory_id = m.id
WHERE e.to_memory_id = $superseded_id
  AND e.edge_type IN ('depends_on', 'cites')
  AND m.archived_at IS NULL;

-- for each dependent, queue a curator re-evaluation task
```

The re-evaluation does **not** auto-archive dependents. It re-runs the
curator strength formula with the dependency change as an input signal,
which may demote (not delete) the dependent memory. Human review before
final archive for `decision` and `rule` tier rows.

### 3.2 Audit ledger — `migrations/00Y_memory_ledger.sql`

```sql
CREATE TABLE devbrain.memory_ledger (
    seq             BIGSERIAL PRIMARY KEY,
    memory_id       UUID NOT NULL,             -- not FK: ledger survives row deletion
    operation       TEXT NOT NULL,             -- 'create'/'update'/'archive'/'restore'/'supersede'
    actor           TEXT NOT NULL,             -- dev_id or 'curator' or 'system'
    project_slug    TEXT NOT NULL,
    payload_hash    BYTEA NOT NULL,            -- sha256(canonicalized memory row)
    prev_hash       BYTEA,                     -- sha256 of previous row's row_hash; NULL for seq=1
    row_hash        BYTEA NOT NULL,            -- sha256(seq || memory_id || operation || actor || project_slug || payload_hash || COALESCE(prev_hash, ''))
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON devbrain.memory_ledger (memory_id);
CREATE INDEX ON devbrain.memory_ledger (project_slug, created_at);
```

**Properties:**

- One row per `memory` write (insert/update/archive). Triggered by AFTER
  triggers on `devbrain.memory`.
- `payload_hash` is sha256 of the canonical JSON of the memory row at write
  time. It does **not** store the content itself — auditors verify by
  re-canonicalizing the `memory` row and comparing.
- `row_hash` chains: each row's hash includes the previous row's hash.
  Mutating an old row breaks all subsequent hashes.
- `verify_chain(start_seq, end_seq)` SQL function walks the chain and
  returns the first seq where `row_hash` doesn't match recomputed value.

**Storage cost:** ~150 bytes/row. At 60k memory rows × ~3 writes each
(create + a few updates / archives) = 180k ledger rows ≈ 30 MB. Negligible.

**HIPAA fit:** auditor produces an integrity report by running
`verify_chain()` on each project. Tampering surfaces the first divergent
seq. Combined with Postgres role-level audit, this gives the audit trail
HIPAA expects.

### 3.3 Compliance postulates — `tests/postulates/`

Not a schema change. A test directory with one file per postulate:

```python
# tests/postulates/test_p1_supersession_cascades.py

POSTULATE = """
P1: When a memory M is superseded by M', every memory that has a
'depends_on' edge to M is re-queued for curator re-evaluation within
the same transaction.
"""

def test_supersession_queues_dependent_for_reeval(db, sample_project):
    m_old = make_memory(db, sample_project, kind='pattern',
                       content='use aiopg for async pg')
    m_dep = make_memory(db, sample_project, kind='issue',
                       content='aiopg connection pool deadlock fix')
    add_dependency(db, from_id=m_dep.id, to_id=m_old.id, edge_type='depends_on')

    m_new = supersede(db, old_id=m_old.id,
                     new_content='use asyncpg for async pg')

    queued = curator_reeval_queue(db, project_slug=sample_project.slug)
    assert m_dep.id in [q.memory_id for q in queued]
```

Run via `pytest tests/postulates/`. Failures are blocking on every PR
that touches the discipline layer.

---

## 4. Curator agent (the existing Phase 3 plan)

Already locked in prior decisions; restated here for context.

- **Runs first in the factory pipeline.** Reads project context, recent
  memories, the spec, and produces a *curator brief*: ranked, severity-tagged
  lessons with provenance ("why this matters: see issue X, decision Y").
- **Cache-warming:** the curator's invocation naturally warms the project-
  level prompt cache used by downstream agents (planner, implementer, evals,
  reviewer). See "Prompt caching + shared context strategy" decision.
- **Re-curates on fix-loop retry:** elevates severity of lessons matching
  the failure, adds new candidates from the failure context.
- **Implementer asymmetry:** initial run sees only curator brief + spec +
  plan. Fix-loop sees brief + spec + plan + previous diff + eval findings
  as actionable items + reviewer verdict as directive. "Coached before,
  directed after" (locked decision 2026-04-15).

**New responsibility from this plan:** when the curator sees a memory marked
`archived_at` because of supersession, it walks `memory_dependencies` to
identify dependents and adds them to a re-evaluation queue. Re-evaluations
run lazily — at most one batch per orchestrator tick — to avoid flooding
the LLM budget on cascading invalidations.

---

## 5. Eval agents

Domain-specialized agents that run after the implementer (in parallel with
each other, sharing the cached project+spec+brief+plan+diff context):

- `eval_security` — auth, injection, secret leakage, dependency CVE check
- `eval_hipaa` (BrightBrain instance only) — PHI handling, audit-log
  completeness
- `eval_perf` — N+1 queries, missing indexes, blocking sync calls in async
- `eval_test` — coverage of the diff, test quality
- `eval_lint` *(programmatic, not LLM)* — runs configured ruff/eslint/etc.

Findings flow into the **factory_artifacts** row as JSON
`{rule_id, severity, file, line, message, fix_hint}` so the
fix-loop implementer can address them as actionable items.

**Lesson graduation pipeline:**

- A `lesson` (memory in `lesson` tier) that the curator includes in N
  consecutive briefs *and* successfully prevents the targeted issue is a
  candidate for promotion to `eval_*` rule.
- A graduated lesson becomes a parameterized check inside one of the eval
  agents (or a new one). The lesson row gets `tier='rule'` and a
  `graduated_at` timestamp.
- Reverse path: an eval rule that fires too often with low precision gets
  demoted back to `lesson` tier.

Three feedback signals (locked decision):

1. *In-brief AND failure happened* — `hit_count++`, `effective_hit_count` NOT
   incremented. Repeat → graduation candidate.
2. *NOT in brief but should have been* — curator ranking failed.
   Refinement agent proposes `applies_when` updates.
3. *In brief AND code was correct first pass* — `effective_hit_count++`,
   strength reinforced.

---

## 6. Compliance rule engine

The `tier='rule'` slice of `devbrain.memory` is the rule engine's runtime
input. Two execution modes:

- **Agent-based (default):** an `eval_*` agent reads a relevant slice of
  rules and applies them to the diff via prompt. Suits fuzzy/semantic rules
  ("don't log PHI"). Cheaper to author, more flexible.
- **Declarative JSON (regulatory):** explicit rule rows with `predicate`
  (regex/AST/SQL pattern), `severity`, `applies_when` constraints. The
  rule engine evaluates them programmatically. Suits hard requirements
  ("no `print()` of values from `phi_*` tables").

Both modes write findings into `factory_artifacts` with the same shape.

The discipline pipeline's contract: any HIPAA-relevant rule **must** have
at least one postulate test in `tests/postulates/` proving its enforcement.
Adding a HIPAA rule without a postulate is a CI failure (lint check).

---

## 7. Implementation order

Suggested PR sequence (each one ships independently, no big-bang):

1. **`migrations/00X_memory_dependencies.sql`** — add the edge table.
   Empty until populated. Update `store` to accept `depends_on` /
   `supersedes` arrays. Backfill from existing `decisions.superseded_by`
   chain.
2. **`migrations/00Y_memory_ledger.sql`** + AFTER triggers on `memory`.
   Ledger fills for new writes only; historical rows remain unledgered
   (acceptable — chain starts at the migration timestamp).
3. **`verify_chain()` SQL function + `devbrain audit verify` CLI.**
   Manually invokable; CI-runnable as a smoke check.
4. **First three postulate tests.** P1 (supersession cascades),
   P2 (archived memory excluded from curator), P3 (HIPAA cross-project
   isolation).
5. **Curator cascade re-evaluation queue.** Curator picks up dependents
   on each tick, re-runs strength formula with dependency-change signal.
6. **First two eval agents.** `eval_security`, `eval_test`. Feed their
   findings into fix-loop. Lesson graduation pipeline goes live.
7. **Compliance rule engine subset.** Five seeded HIPAA rules in
   declarative JSON form. Each ships with a postulate test.

Stop at step 7 for v3.0. Steps 8+ (more eval agents, rule refinement
agent, demotion pipeline) are Phase 3.x increments.

---

## 8. Locked decisions referenced

- **Apache AGE** (Postgres extension) over Neo4j — local-first simplicity
  trumps Neo4j's traversal performance at our scale. Atlas's Neo4j-coupled
  Ripple Engine code is intentionally NOT borrowed; the pattern is
  reimplemented on top of the AGE/edge-table substrate.
- **Per-project lesson scope with opt-in cross-project promotion.**
  Cross-project would let HIPAA lessons leak into personal projects.
- **Agent-based eval as default; declarative JSON only for regulatory.**
  Custom Python plugins are a rare escape hatch.
- **Strength formula** stays Enso-derived (`decay × retrievalBoost ×
  emotionalMultiplier × usageFeedback`). Atlas doesn't change this.
- **Two-tier prompt cache** (project-extended-TTL + job-ephemeral). Curator
  warms project cache. The cascade-reevaluation queue should batch within
  a single curator invocation when possible to ride the same warm cache.

---

## 9. Open questions

- **Audit ledger encryption.** Should `payload_hash` be HMAC'd with a
  per-project key so a DB-only attacker who doesn't have the key can't
  forge consistent hashes? Probably yes for HIPAA. Phase 3 ships the
  plain-SHA256 chain; HMAC is a Phase 3.1 add-on.
- **Cascade depth limit.** A supersession on a foundational decision
  could cascade to hundreds of dependents. Cap at depth 2 with a
  follow-up curator pass for deeper levels? Or budget by total LLM tokens
  per cascade?
- **Ledger replay for cross-instance migration.** When BrightBrain →
  another instance migration happens, do we replay the ledger to verify
  integrity in the new home? Probably yes, but that's a separate
  migration tooling concern.
- **Postulate authoring UX.** Is there a `devbrain postulate add`
  CLI scaffolder that drops a pytest file with the right fixtures? Nice
  to have, deferred.

---

## 10. Non-goals

- **Atlas codebase port.** Their Neo4j stack, Obsidian/Limitless/iMessage
  ingestion, FastAPI surface, MCP wrapper, and gRPC bridge are all
  intentionally out of scope. We borrow concepts only.
- **AGM postulate strict compliance.** DevBrain is not a belief-revision
  research project. We borrow the *methodology* (postulate-then-test) and
  *one mechanism* (dependency cascade), not the formal AGM compliance bar.
- **Universal supersession metadata.** We're not retrofitting every legacy
  `decisions.superseded_by` chain into the new edge table on day one.
  Backfill happens lazily per project as needed.

---

## 11. References

- [Atlas — RichSchefren/atlas](https://github.com/RichSchefren/atlas) —
  the source of Ripple, hash-chain ledger, and postulate-test pattern.
- [AGM postulates](https://en.wikipedia.org/wiki/Belief_revision) —
  Alchourrón, Gärdenfors, Makinson 1985.
- DevBrain decisions (deep_search):
  - "Locked design decisions for DevBrain v2 architecture" (2026-04-15)
  - "Implementer influence pattern: coached before, directed after" (2026-04-15)
  - "Prompt caching + shared context strategy for agent pipeline" (2026-04-15)
- DevBrain phase status — see `ARCHITECTURE.md` §9 and
  `docs/MEMORY_MODEL.md`.
