# Atlas Step 5 — Curator Agent + Cascade Re-evaluation Queue: Design

> **Status:** Design locked via brainstorm session 2026-05-03 → 2026-05-04.
> Implementation plan to follow.
>
> **References:**
> - `docs/plans/2026-05-02-session-continuation-playbook.md` — Phase 3 playbook
> - PR #67 (`docs/phase-3-discipline-atlas`) — original design doc
> - DevBrain decision `fc1a62bb` — compliance per-project profiles refactor
>
> **Verification gate:** Existing postulates P1 + P2 flip from `xfail(strict=True)`
> to passing. Five new postulates ship.

---

## 1. Architecture overview

The curator is **two cooperating components plus an API extension**, sharing
one DB substrate. None is a long-running new daemon — they piggyback on
processes that already exist.

```
┌─────────────────────────────────────────────────────────────────────┐
│                            DevBrain DB                              │
│  devbrain.memory ─── memory_dependencies ─── memory_ledger          │
│         │                     │                                     │
│         │                     │                                     │
│  ┌──────▼──────┐      ┌───────▼────────┐                            │
│  │ curator_re_ │      │  factory_      │                            │
│  │ eval_queue  │      │  artifacts     │                            │
│  └──────┬──────┘      └────────────────┘                            │
└─────────┼───────────────────────────────────────────────────────────┘
          │ drains
          ▼
┌─────────────────────────┐    ┌─────────────────────────┐
│  Cascade Worker         │    │  Brief Generator        │
│  (in factory/orches-    │    │  (sync function called  │
│   trator process)       │    │   at QUEUED→PLANNING)   │
│                         │    │                         │
│  - Pure function calls  │    │  - Pure function calls  │
│  - Strength formula     │    │  - Filters + ranks      │
│  - No LLM               │    │  - Emits CuratorBrief   │
└─────────────────────────┘    └─────────────────────────┘

         (end_session MCP tool — extended)
                     │
                     ▼ judgment payload from calling agent
              writes to memory + edges + cascade_queue
              (NO separate LLM agent — calling agent IS the judge)
```

### Three triggers, three pathways, one substrate

1. **Any `store()`** that mutates a memory in a cascading way (writes a
   `supersedes` edge, sets `archived_at`, mutates `applies_when`) enqueues
   affected dependents in `curator_re_eval_queue`. Cascade worker drains
   whenever DevBrain is running, regardless of factory activity.
   **Mechanical, fast, no LLM.**

2. **Factory job `QUEUED → PLANNING` transition** calls
   `generate_brief(job_id)` synchronously. Brief Generator filters memories
   (compliance profiles, applies_when match), ranks by current strength,
   returns `CuratorBrief v1.0`. **Pure function, no LLM in v3.0** (could
   become LLM-driven later in Phase 3.x without breaking the contract).

3. **`end_session()`** — calling agent passes `cascade_decisions` +
   `new_relationships` + `lesson_candidates` as part of the call. DevBrain
   persists them as side-effects. **Calling agent IS the judgment source;
   no separate LLM call.** Avoids handoff to a second agent that would
   only have a degraded summary of session context.

### Forward-compatibility properties (per playbook §9)

- Brief is a versioned Pydantic model. Step 6 eval agents and (eventually)
  Phase 6 cognify consume the same shape.
- Cascade worker is a pure function callable from anywhere. Phase 6 cognify
  can invoke the same function offline for batch reweighting.
- end_session enrichment generalizes to "any caller can volunteer judgment"
  — including the cognify daemon when it ships.
- `compliance_profiles` and `applies_when` stay flat on the row. Edge
  semantics belong in `memory_edges` (Phase 5), not on the row.

---

## 2. Data model additions

### Migrations

```sql
-- Queue table — drained by cascade worker
CREATE TABLE devbrain.curator_re_eval_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id UUID NOT NULL REFERENCES devbrain.memory(id) ON DELETE CASCADE,
    cascade_source_id UUID NOT NULL REFERENCES devbrain.memory(id),
    edge_type TEXT NOT NULL CHECK (edge_type IN
        ('supersedes','archived_at','applies_when')),
    enqueued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
CREATE INDEX idx_re_eval_queue_fifo ON devbrain.curator_re_eval_queue (enqueued_at);

-- Audit: when did cascade worker last touch this row
ALTER TABLE devbrain.memory ADD COLUMN last_cascade_at TIMESTAMPTZ;

-- Brief preserved per job — every phase reads the same snapshot
ALTER TABLE devbrain.factory_jobs ADD COLUMN curator_brief JSONB;
```

Queue drainage uses `SELECT … FOR UPDATE SKIP LOCKED` — no claim columns,
no stuck-worker recovery needed, multiple workers safe by default.

The `curator_brief` column on `factory_jobs` is the cache: brief is generated
once at `QUEUED → PLANNING`, written to that column, and every phase
(planner, implementer, reviewer, QA) reads from it. Guarantees consistency
across phases and gives a full audit trail post-job.

### Pydantic types — `factory/curator/types.py`

```python
class MemoryRef(BaseModel):
    id: UUID
    kind: Literal["chunk","decision","pattern","issue","session_summary"]
    title: str | None
    content_excerpt: str  # first ~500 chars
    tier: Literal["memory","lesson","rule"]
    strength: Decimal
    last_cascade_at: datetime | None

class CascadeNote(BaseModel):
    affected_memory_id: UUID
    cascade_source_id: UUID
    edge_type: Literal["supersedes","archived_at","applies_when"]
    occurred_at: datetime
    summary: str  # "Rule R6 was superseded 2h ago"

class CuratorBrief(BaseModel):
    version: Literal["1.0"]
    job_id: UUID
    project_id: UUID
    rules: list[MemoryRef]              # tier=rule, profile-filtered
    lessons: list[MemoryRef]            # tier=lesson, ranked
    relevant_decisions: list[MemoryRef] # tier=memory, applies_when match
    recent_cascade_signals: list[CascadeNote]
    generated_at: datetime
```

### `end_session()` MCP tool — additive params

```python
end_session(
    summary, files_changed, next_steps, decisions_made,    # existing
    cascade_decisions: list[CascadeDecision] = [],         # NEW
    new_relationships: list[NewEdge] = [],                 # NEW
    lesson_candidates: list[LessonCandidate] = [],         # NEW
)
```

Old callers (Claude Code today) keep working; new callers volunteer judgment.
`CascadeDecision.action` is one of `promote | merge | contradict | refine |
no_action`.

---

## 3. Components

```
factory/curator/
├── __init__.py
├── types.py         # Pydantic models (§2)
├── strength.py      # Pure formula — penalty, freshness decay
├── worker.py        # Queue drainer (runs in factory orchestrator process)
├── brief.py         # Brief generator (sync, called by state machine)
└── end_session.py   # Handlers for cascade_decisions / new_relationships / lesson_candidates
```

### `strength.py` — pure functions, no DB dependency

```python
PENALTY = {"supersedes": 0.40, "archived_at": 0.25, "applies_when": 0.10}

def freshness_decay(age_s: float) -> float:
    return 0.5 ** (age_s / 86400)        # half-life 24h

def cascade_penalty(edge_type, age_s) -> Decimal:
    return Decimal(PENALTY[edge_type] * freshness_decay(age_s))

def apply_cascade(strength, edge_type, age_s) -> Decimal:
    return max(Decimal("0"), strength - cascade_penalty(edge_type, age_s))
```

Pure → trivially unit-testable, callable from Phase 6 cognify offline.

### `worker.py` — queue drainer

Runs in the existing factory orchestrator process. Adds one sibling poll
alongside the existing `factory_jobs` poll. Each batch:

1. `SELECT … FOR UPDATE SKIP LOCKED` claims up to N rows
2. For each: load memory, compute `new_strength`, UPDATE memory (sets
   `last_cascade_at`), DELETE queue row
3. **Multi-hop:** if cascade was significant (penalty > 0.05 after freshness
   decay), enqueue the row's *own* dependents

On exception: `attempt_count++`, write `last_error`, leave row in queue.
After 3 failures, row gets `attempt_count=3` and is skipped — surfaces in a
`devbrain curator queue-stuck` CLI report.

### `brief.py` — synchronous brief generator

```python
def generate_brief(conn, job_id, project_id, spec) -> CuratorBrief:
    profiles = load_enabled_profiles(conn, project_id)
    rules = load_rules(conn, profiles)                  # tier='rule', filtered
    lessons = load_lessons(conn, top_n=20)              # tier='lesson', strength desc
    decisions = load_decisions_matching(conn, spec)     # applies_when match
    cascades = load_recent_cascades(conn, since="24h")  # CascadeNote list
    brief = CuratorBrief(...)
    persist_to_factory_job(conn, job_id, brief)
    return brief
```

The applies_when matcher is naive in v3.0 — substring/keyword match against
spec text. Phase 3.x can swap in a smarter matcher (semantic similarity,
structured field match) without changing the function signature.

### `end_session.py` — handlers

Three small functions, one per new MCP param. Each writes to substrate as
side-effects of `end_session()`:
- `cascade_decisions` → updates `strength`, `tier`, or `archived_at` per
  agent's call; enqueues cascades for any new edges
- `new_relationships` → inserts into `memory_dependencies`
- `lesson_candidates` → inserts new `tier='lesson'` rows

### Integration points

1. **`factory/state_machine.py`** — `transition_queued_to_planning()` calls
   `generate_brief()` synchronously before flipping status.
2. **`mcp-server/src/tools/store.ts` + `end_session.ts`** — TypeScript MCP
   server adds enqueue logic when `store()` writes cascading mutations, and
   accepts the new optional `end_session()` params.

---

## 4. Data flow — happy-path trace

**Setup:** Project has `compliance_profiles_enabled=['hipaa']`. Three
relevant memories already in substrate:
- `R6` (tier='rule', HIPAA, "PHI must not appear in unstructured logs")
- `L42` (tier='lesson', strength=0.85, `depends_on=[R6]`)
- `L99` (tier='lesson', strength=0.70, `depends_on=[L42]`)

### Pathway 1 — Agent stores a supersession

```
agent calls store(R6_v2, tier='rule', supersedes=[R6])
  ├─ INSERT R6_v2 + edge (R6_v2 ─supersedes→ R6)
  ├─ memory_ledger AFTER trigger logs both writes
  ├─ Cascade detection: R6 was superseded → walk memory_dependencies
  │   └─ Found L42 depends_on R6
  └─ INSERT curator_re_eval_queue (memory=L42, source=R6, edge=supersedes)
store() returns in ~10ms. No LLM call.
```

### Pathway 2 — Cascade worker drains (next 5s poll)

```
worker.drain_one_batch()
  ├─ SELECT … FOR UPDATE SKIP LOCKED → claims L42 queue row
  ├─ age_s = 30s; penalty = 0.40 × 0.5^(30/86400) ≈ 0.40
  ├─ new_strength = max(0, 0.85 − 0.40) = 0.45
  ├─ UPDATE memory SET strength=0.45, last_cascade_at=now WHERE id=L42
  ├─ DELETE queue row
  └─ Multi-hop: 0.40 > 0.05 threshold → enqueue L99 (depends_on L42)
next batch: drains L99 with smaller penalty (freshness decay + edge-type)
```

### Pathway 3 — Factory job 30 min later

```
factory job submitted (touches phi_audit_log.py)
state_machine: QUEUED → PLANNING
  ├─ generate_brief(job.id, project.id, spec):
  │   ├─ profiles = ['hipaa']
  │   ├─ rules: includes R6_v2 (current); R6 excluded (archived)
  │   ├─ lessons: L42 ranked lower (strength=0.45 now); L99 lower still
  │   ├─ decisions: matched by applies_when ∩ spec
  │   ├─ cascades: CascadeNote("L42 weakened — R6 superseded 30 min ago")
  │   └─ persist to factory_jobs.curator_brief
  └─ status = PLANNING
planner reads brief, sees cascade signal, downweights L42-derived guidance
```

### Pathway 4 — `end_session()` enriches

```
agent calls end_session(
    summary=...,
    cascade_decisions=[{
        memory_id: L42,
        action: "refine",
        rationale: "R6's supersession means L42's applies_when is too broad"
    }],
    lesson_candidates=[{
        title: "Always check audit_log writes when refactoring PHI handlers",
        applies_when: {files: ["phi_*.py"]},
        compliance_profiles: ["hipaa"]
    }]
)
  ├─ handle_cascade_decisions: marks L42 for refinement (Step 6 picks up)
  └─ handle_lesson_candidates: INSERT new tier='lesson' row
```

One pure-function worker, one sync brief generator, one enriched MCP tool —
no separate LLM agent. Multi-hop converges. Audit ledger has every write.

---

## 5. Edge cases + invariants

### Cycle prevention in dependency graph

A → B → A is possible. Worker only enqueues a dependent if its
`last_cascade_at` is older than the cascade source's mutation time. A row
already touched by *this* cascade wave is skipped. Cycles converge in one
trip around.

### Archived-between-enqueue-and-drain

Worker behavior when target has `archived_at IS NOT NULL`:
- Skip the strength update
- Still DELETE the queue row
- Don't enqueue *its* dependents — archived memories don't propagate

### Brief generation failure

If `generate_brief()` raises during `QUEUED → PLANNING`:
- Write `last_error` to the job row, `error_count++`
- Job stays in `QUEUED`
- After 3 attempts, transition to `BLOCKED`
- Admin can `devbrain factory unblock <job-id>` to retry, or write empty
  brief manually

### Compliance profile semantics — explicit opt-in

Rule with `compliance_profiles = NULL` or `[]` applies to **no** project.
Defaults must be safe — projects only get rules they explicitly enabled.
Postulate P6 (playbook §5) enforces.

### Cross-project isolation in `end_session()`

Validate every memory_id in the payload belongs to the session's project.
Reject the entire payload on mismatch — don't partial-apply. P3 keeps
passing.

### Concurrent stores writing the same edge

`memory_dependencies` UNIQUE constraint on `(from_id, to_id, edge_type)`.
Use `INSERT … ON CONFLICT DO NOTHING`. Cascade enqueue still fires on the
winning insert.

### Idempotency of `end_session()`

Use `session_id` as idempotency key. Second call is a no-op returning the
first call's result.

### Worker stuck-row policy

Queue rows with `attempt_count >= 3` are skipped automatically. CLI
`devbrain curator queue-stuck` lists them with `last_error` for triage.

---

## 6. Testing

### Postulates — `tests/postulates/`

**Flipped from `xfail(strict=True)` to passing:**
- **P1** — supersession cascade re-eval
- **P2** — archived memory excluded from curator brief

**New in Step 5:**
- **P_cycle** — dependency cycle (A→B→A) converges in one wave
- **P_archived_mid_cascade** — archived target during drain → DELETE without
  propagating
- **P_end_session_isolation** — cross-project payload rejected wholesale
- **P_end_session_idempotent** — same `session_id` twice = same observable
  state
- **P_stuck_surface-able** — `attempt_count >= 3` rows visible in CLI

### Unit tests — pure functions

`tests/unit/test_curator_strength.py` (100% coverage of `strength.py`):
- Penalty ordering: `supersedes > archived_at > applies_when`
- Freshness half-life at 24h
- `apply_cascade` clamped at 0
- Strong memory survives single cascade with non-trivial residual

`tests/unit/test_curator_types.py`: Pydantic round-trips, version field
rejection of unknown versions.

### Integration tests — full flow

`tests/integration/test_curator_e2e.py`:
- Store with supersession → poll until worker drains → assert downstream
  strength dropped → start factory job → assert brief contains CascadeNote
  → end_session with judgment payload → assert side-effects persisted
- Concurrency: two workers running, no row processed twice

### Coverage gate

Step 5 PR(s) must merge with:
- All existing postulates passing (P1+P2+P3 + 5 new)
- 100% coverage on `factory/curator/strength.py` and `factory/curator/types.py`
- `factory/curator/worker.py` and `factory/curator/brief.py` ≥ 85% coverage
- DB-available CI workflow green (the one Step 4 deferred — Step 5 enables)

---

## 7. Locked design decisions

| # | Decision | Reason |
|---|---|---|
| 1 | Hybrid trigger model: store→queue→worker (mechanical) + end_session enrichment (judgment from calling agent) | `store()` stays fast (~10ms); judgment uses full session context, no degraded summary handoff |
| 2 | Additive bounded penalty, edge-typed, freshness-decayed (24h half-life) | Multi-hop friendly; preserves earned strength; deterministic for postulate testing |
| 3 | Sectioned `CuratorBrief` v1.0 (rules / lessons / relevant_decisions / recent_cascade_signals) | Clear consumption signal for planner + Step 6 evals; versioned for forward evolution |
| 4 | Brief cached on `factory_jobs.curator_brief` JSONB column | Every phase of a job reads identical brief; full audit trail post-job |
| 5 | `end_session()` API additive — old callers keep working | Backward compat; new callers volunteer judgment |
| 6 | Worker runs in existing factory orchestrator process | No new daemon; matches existing poll-DB-and-act pattern |
| 7 | Compliance profile semantics: empty = excluded (must opt in) | Safe defaults; P6 enforces |
| 8 | Pure-function strength formula | Callable from Phase 6 cognify offline (playbook §9) |

---

## 8. Open at implementation time (deferred from design)

Small enough to resolve during PR review or while writing the implementation
plan:

- **Worker batch size** — start at 50, tune based on observed lag
- **Worker poll interval** — start at 5s, configurable via
  `config/devbrain.yaml`
- **Multi-hop penalty threshold** — currently 0.05 (don't propagate if
  residual penalty smaller); revisit after first real run
- **Lesson `top_n` in brief** — currently 20; revisit when actual brief
  rendering is observed
- **Naive applies_when matcher details** — substring vs. token vs.
  fuzzy-match; pick at implementation
- **Stuck-queue alert threshold** — what count of `attempt_count >= 3` rows
  triggers a notification?

---

## 9. Implementation order — sub-PRs within Step 5

1. **5a — migration + types.** `curator_re_eval_queue` + `last_cascade_at`
   + `factory_jobs.curator_brief`. Pydantic models in `factory/curator/types.py`.
   Unit tests for round-trips.
2. **5b — strength.py.** Pure formula + 100% coverage unit tests.
3. **5c — worker.py.** Queue drainer + multi-hop + integration into
   factory orchestrator. P_cycle, P_archived_mid_cascade, P_stuck_surface-able
   postulates.
4. **5d — brief.py.** Generator + state machine integration (`QUEUED →
   PLANNING` calls it). P1 + P2 flip green.
5. **5e — end_session enrichment.** MCP server tool extension +
   `end_session.py` handlers. P_end_session_isolation +
   P_end_session_idempotent postulates.
6. **5f — DB-available CI workflow.** Enable the deferred postulate-running
   CI job (was Step 4 follow-up). All postulates green in CI.

Each sub-PR is independently reviewable. After 5f, Step 5 is complete and
the gate to Step 6 (eval agents) is open.
