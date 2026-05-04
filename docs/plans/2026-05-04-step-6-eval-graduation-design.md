# Atlas Step 6 — Eval Agents + Lesson Graduation Pipeline: Design

> **Status:** Design locked via brainstorm session 2026-05-04. Implementation
> plan to follow.
>
> **References:**
> - `docs/plans/2026-05-02-session-continuation-playbook.md` — Phase 3 playbook
> - `docs/plans/2026-05-04-step-5-curator-design.md` — Step 5 design (substrate)
> - DevBrain decision `bd14d59f` — Step 5 complete
>
> **Verification gate:** Two new postulates ship — P4 (lesson graduation), P5
> (rule demotion). All 8 existing postulates from Step 5 still pass.

---

## 1. Architecture overview

Step 6 ships **two eval agents + a graduation pipeline + a refinement path**,
all consuming the substrate Step 5 left behind.

```
┌─────────────────────────────────────────────────────────────────────┐
│                         DevBrain DB                                 │
│  devbrain.memory ─── memory_dependencies ─── memory_ledger          │
│         │              (Step 5 substrate)                           │
│         │                                                           │
│  ┌──────▼──────┐      ┌─────────────────┐                           │
│  │  current_   │      │  factory_       │                           │
│  │  streak     │      │  artifacts      │                           │
│  │  (NEW)      │      │  (Step 5)       │                           │
│  └──────┬──────┘      └────────▲────────┘                           │
└─────────┼──────────────────────┼────────────────────────────────────┘
          │                      │ findings
          │                      │
          │              ┌───────┴────────┐
          │              │  Eval runner   │
          │              │  (sequential   │
          │              │  warm-cache)   │
          │              │                │
          │              │  ├─ eval_sec   │
          │              │  └─ eval_test  │
          │              └───────┬────────┘
          │                      │
          │                      ▼ signals 1+3
          │              ┌────────────────┐
          │              │  Graduation    │
          │              │  pipeline      │
          │              │                │
          │              │  current_streak│
          │              │  ↑↓ → tier     │
          │              │     transition │
          └──────────────┤      ↓         │
                         │  ledger row    │
                         └────────────────┘
                                  │
                                  ▼ signal 2 (NOT in brief)
                         ┌────────────────┐
                         │   Refinement   │
                         │  (curator      │
                         │  self-introsp.)│
                         │                │
                         │  applies_when  │
                         │  widening      │
                         └────────────────┘
```

### Three triggers, three pathways

1. **State machine `IMPLEMENTING → REVIEWING`** → eval runner fires.
   Sequential calls: eval_security then eval_test, sharing a warm prompt
   cache (project + spec + brief + plan + diff cached once, both agents
   reuse).

2. **End of eval phase** → graduation pipeline reads findings + brief,
   fires three feedback signals per memory referenced:
   - In-brief AND fired (eval found a violation): `hit_count++`, **streak reset to 0**
   - In-brief AND clean (no violation): `effective_hit_count++`, **`streak++`**, `last_hit = NOW()`
   - NOT in-brief but eval found a relevant violation: queue for refinement (signal #2)

3. **End of REVIEWING phase** (after graduation) → curator self-introspection
   correlates "findings whose relevant memories weren't in the brief" with
   `applies_when` patterns → proposes widening updates. Supplemental path:
   `end_session()` enrichment can volunteer richer refinement when the calling
   agent has judgment.

### Forward-compatibility (per playbook §9)

- Eval agents consume `CuratorBrief v1.0` unchanged. Step 6 doesn't bump the
  brief schema.
- Graduation pipeline writes every tier transition to `memory_ledger`. The
  ledger contract from Step 2 is unchanged.
- Refinement updates to `applies_when` stay flat (no edge semantics on the
  row — those belong in Phase 5 `memory_edges`).
- Eval runner is a function callable from anywhere (cognify in Phase 6 can
  invoke the same prompts on a schedule).

---

## 2. Data model additions

### Migration 019

```sql
-- Atlas Step 6 — Lesson graduation tracking
-- ============================================================================
--
-- Adds three columns to devbrain.memory for the graduation pipeline:
--   1. current_streak — count of consecutive successful preventions
--      (signal #3 increments, signal #1 resets). Drives the N=3 graduation
--      threshold.
--   2. graduated_at — timestamp when tier transitioned 'lesson' -> 'rule'.
--      NULL for memories that have never graduated.
--   3. demoted_at — timestamp when tier transitioned 'rule' -> 'lesson'
--      due to low-precision firing. NULL for memories that haven't been
--      demoted.
--
-- The streak alone doesn't determine graduation — the graduation pipeline
-- also requires last_hit > NOW() - INTERVAL '90 days' so stale lessons
-- don't graduate just because they kept their streak from when they were
-- active.

ALTER TABLE devbrain.memory
    ADD COLUMN IF NOT EXISTS current_streak INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS graduated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS demoted_at TIMESTAMPTZ;

-- Index for graduation candidate query
CREATE INDEX IF NOT EXISTS idx_memory_graduation_candidates
    ON devbrain.memory (last_hit DESC)
    WHERE tier = 'lesson' AND current_streak >= 3 AND archived_at IS NULL;
```

### Pydantic types — `factory/curator/eval/types.py`

```python
class EvalFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_id: UUID | None  # NULL if finding is from a heuristic, not a memory row
    severity: Literal["critical", "important", "minor"]
    file: str
    line: int | None
    message: str
    fix_hint: str
    relevant_memory_id: UUID | None  # which memory in brief surfaced this; NULL if missed


class EvalResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: Literal["1.0"]
    job_id: UUID
    agent_name: Literal["eval_security", "eval_test"]
    findings: list[EvalFinding]
    elapsed_ms: int
    started_at: datetime
```

### `factory_artifacts` integration

Eval findings persist as JSON in the existing `factory_artifacts` table
(introduced in Step 5's substrate). One row per finding:

```json
{
  "rule_id": "uuid-or-null",
  "severity": "critical|important|minor",
  "file": "factory/curator/eval/runner.py",
  "line": 42,
  "message": "...",
  "fix_hint": "..."
}
```

The fix-loop implementer (existing Step 5 mechanism) reads them as
actionable items.

---

## 3. Components

```
factory/curator/
├── eval/                              # NEW
│   ├── __init__.py
│   ├── types.py                       # Pydantic models (§2)
│   ├── runner.py                      # sequential warm-cache invocation
│   ├── prompts/
│   │   ├── eval_security.md           # eval_security system prompt
│   │   └── eval_test.md               # eval_test system prompt
│   ├── eval_security.py               # parser + finding emission
│   └── eval_test.py                   # parser + finding emission
├── graduation.py                      # NEW — three-signal feedback loop
└── refinement.py                      # NEW — curator self-introspection
```

### `runner.py` — sequential warm-cache eval

```python
def run_evals(conn, job_id, brief, plan, diff) -> list[EvalResult]:
    """Run eval_security then eval_test sharing a warm prompt cache.

    Spawns ONE claude process. First call instantiates the cache with
    project + spec + brief + plan + diff. Second call hits cached input
    at ~10% input-token cost.
    """
    cache_input = _build_cached_context(brief, plan, diff)

    sec_result = _invoke_eval(
        prompt_path="prompts/eval_security.md",
        agent_name="eval_security",
        cache_input=cache_input,
    )
    test_result = _invoke_eval(
        prompt_path="prompts/eval_test.md",
        agent_name="eval_test",
        cache_input=cache_input,
        # Reuses cache — second call's input cost ~10% of first.
    )

    _persist_findings(conn, job_id, [sec_result, test_result])
    return [sec_result, test_result]
```

### `graduation.py` — three feedback signals

```python
GRADUATION_STREAK_THRESHOLD = 3
GRADUATION_FRESHNESS_WINDOW = "90 days"
DEMOTION_PRECISION_THRESHOLD = 0.50
DEMOTION_WINDOW = "30 days"


def apply_feedback_signals(conn, job_id, brief, eval_results):
    """For each memory referenced in brief, fire the appropriate signal.

    Walks brief.rules + brief.lessons + brief.relevant_decisions.
    Cross-references findings to determine which memories were in-brief
    AND fired (signal 1), in-brief AND clean (signal 3), or NOT in-brief
    but should have been (signal 2 — queued for refinement).
    """
    in_brief_ids = _collect_brief_memory_ids(brief)
    findings_by_memory = _index_findings_by_memory(eval_results)

    for mid in in_brief_ids:
        if mid in findings_by_memory:
            # Signal 1: in-brief AND failure
            _signal_failure(conn, mid)
        else:
            # Signal 3: in-brief AND code correct
            _signal_success(conn, mid)

    # Signal 2: findings whose relevant_memory_id is NOT in brief
    for finding in _all_findings(eval_results):
        if finding.relevant_memory_id and finding.relevant_memory_id not in in_brief_ids:
            _queue_refinement(conn, finding)


def _signal_failure(conn, memory_id):
    """In-brief AND failure — reset streak, increment hit_count."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory "
            "SET hit_count = hit_count + 1, current_streak = 0 "
            "WHERE id = %s",
            (memory_id,),
        )
    conn.commit()


def _signal_success(conn, memory_id):
    """In-brief AND clean — streak++, effective_hit_count++, check graduation."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory "
            "SET effective_hit_count = effective_hit_count + 1, "
            "    current_streak = current_streak + 1, "
            "    last_hit = NOW() "
            "WHERE id = %s "
            "RETURNING tier, current_streak, last_hit",
            (memory_id,),
        )
        tier, streak, last_hit = cur.fetchone()

        if tier == "lesson" and streak >= GRADUATION_STREAK_THRESHOLD:
            _graduate(conn, memory_id)
    conn.commit()


def _graduate(conn, memory_id):
    """Promote tier='lesson' to tier='rule', record in ledger."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory "
            "SET tier = 'rule', graduated_at = NOW() "
            "WHERE id = %s AND tier = 'lesson'",
            (memory_id,),
        )
    # AFTER trigger on memory writes ledger row automatically (Step 2 substrate)


def demote_low_precision_rules(conn, project_id):
    """Periodic sweep: rules firing with < 50% precision over 30 days demote.

    Called from end of REVIEWING phase. Tier transition recorded in ledger.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE devbrain.memory
            SET tier = 'lesson', demoted_at = NOW(), current_streak = 0
            WHERE tier = 'rule'
              AND project_id = %s
              AND last_hit > NOW() - INTERVAL '{DEMOTION_WINDOW}'
              AND CAST(effective_hit_count AS FLOAT)
                  / NULLIF(hit_count + effective_hit_count, 0)
                  < %s
            """,
            (project_id, DEMOTION_PRECISION_THRESHOLD),
        )
    conn.commit()
```

### `refinement.py` — curator self-introspection

```python
def refine_applies_when(conn, project_id):
    """Process queued refinements (signal #2) and propose applies_when widening.

    For each finding whose relevant_memory_id was NOT in the brief, the
    curator's ranking failed. The refinement path widens the memory's
    applies_when so future briefs include it under similar contexts.

    v3.0: simple keyword extraction from finding.file + finding.message.
    Phase 3.x: smarter heuristic or LLM-driven proposal.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT memory_id, file_pattern, keywords "
            "FROM devbrain.refinement_queue WHERE applied_at IS NULL"
        )
        for memory_id, file_pattern, keywords in cur.fetchall():
            _widen_applies_when(conn, memory_id, file_pattern, keywords)
            cur.execute(
                "UPDATE devbrain.refinement_queue "
                "SET applied_at = NOW() WHERE memory_id = %s",
                (memory_id,),
            )
    conn.commit()
```

### State machine integration

`factory/state_machine.py` — `transition(IMPLEMENTING → REVIEWING)` adds:

```python
if (JobStatus(job.status) == JobStatus.IMPLEMENTING
        and new_status == JobStatus.REVIEWING):
    from curator.eval.runner import run_evals
    from curator.graduation import apply_feedback_signals, demote_low_precision_rules
    from curator.refinement import refine_applies_when

    brief = _load_brief(self.conn, job.id)
    plan = _load_plan(self.conn, job.id)
    diff = _load_diff(self.conn, job.id)
    eval_results = run_evals(self.conn, job.id, brief, plan, diff)
    apply_feedback_signals(self.conn, job.id, brief, eval_results)
    refine_applies_when(self.conn, job.project_id)
    demote_low_precision_rules(self.conn, job.project_id)
```

---

## 4. Data flow — happy-path trace

**Setup:** Project's brief (generated at QUEUED→PLANNING) included:
- Rule R6: "PHI must not appear in unstructured logs"
- Lesson L42: "Always validate webhook signatures" (current_streak=2)
- Lesson L99: "Use parameterized SQL queries" (current_streak=2)

The implementer commits a diff that:
- Validates a webhook signature correctly (L42 prevented a violation)
- Uses parameterized SQL (L99 prevented a violation)
- Logs a `phi_audit_log` row with redacted PHI (R6 enforced)
- Has a missing test for a new edge case (eval_test will catch this, no memory in brief covered it)

### Pathway 1 — Eval runner

```
state_machine: IMPLEMENTING → REVIEWING
  ├─ run_evals(brief, plan, diff)
  │   ├─ spawn claude with cached context
  │   ├─ eval_security: 0 findings (R6, L42, L99 all clean)
  │   └─ eval_test: 1 finding (missing test, no relevant_memory_id)
  └─ persist findings to factory_artifacts (1 row)
```

### Pathway 2 — Graduation pipeline

```
apply_feedback_signals:
  ├─ R6 (in-brief, no finding): _signal_success
  │     hit_count: 7 → 7
  │     effective_hit_count: 12 → 13
  │     current_streak: tier='rule' so no graduation check
  ├─ L42 (in-brief, no finding): _signal_success
  │     effective_hit_count: 4 → 5
  │     current_streak: 2 → 3 ✓ THRESHOLD HIT
  │     last_hit: NOW
  │     → _graduate(L42)
  │         tier: 'lesson' → 'rule'
  │         graduated_at: NOW
  │         memory_ledger row written by AFTER trigger
  ├─ L99 (in-brief, no finding): _signal_success
  │     current_streak: 2 → 3 ✓ THRESHOLD HIT
  │     → _graduate(L99)
  └─ eval_test finding has relevant_memory_id=NULL — skip signal 2
```

After this job: L42 and L99 are now `tier='rule'`. They'll appear in the
`rules` section of future briefs (with compliance_profile filtering once
Step 7 ships) and the curator no longer treats them as soft guidance.

### Pathway 3 — Refinement (no-op this job)

```
refine_applies_when:
  refinement_queue is empty (signal 2 didn't fire — eval_test finding
  had no relevant_memory_id)
```

### Pathway 4 — Demotion sweep (no-op this job)

```
demote_low_precision_rules:
  No rules currently in low-precision state. Sweep returns 0 demoted.
```

---

## 5. Edge cases + invariants

### Eval agent failure

If `eval_security` errors (network, API quota, prompt eval timeout), the
graduation pipeline still runs against `eval_test`'s findings — feedback
signals still fire for in-brief memories the surviving agent covered.
Failed agent's findings recorded as empty list with an `error` annotation
in `factory_artifacts`.

### Non-deterministic LLM findings

A small percentage of eval runs may emit different findings on the same
diff (LLM noise). Acceptable: graduation/demotion thresholds use windows
(90 days for graduation freshness, 30 days for demotion precision) so
single-run noise smooths out.

### Concurrent graduation transactions

`current_streak` updates use `UPDATE ... RETURNING` to atomically read +
update. Race-free under standard PG MVCC.

### Demotion of recently-graduated rules

A rule that just graduated (`graduated_at` set within the demotion window)
might be demoted on its next firing if it produces a false positive. That's
correct — graduation is reversible by design. The ledger preserves the full
history of transitions.

### Refinement-queue overflow

If many findings accumulate without `applies_when` widening keeping pace,
the queue grows unboundedly. Cap: `applied_at IS NULL AND created_at >
NOW() - INTERVAL '7 days'` filters old un-applied entries from periodic
processing. Failed widenings get `applied_at` set with an `error` field.

### Cross-project safety

All graduation/refinement queries are scoped by `project_id`. P3 (HIPAA
cross-project isolation) postulate from Step 4 still holds — Step 6 doesn't
introduce cross-project memory mutations.

---

## 6. Testing

### Postulates

**New in Step 6:**
- **P4 — lesson graduation.** A `tier='lesson'` row included in 3
  consecutive briefs followed by 3 successful preventions (no eval
  findings) within a 90-day window transitions to `tier='rule'`,
  `graduated_at` is set, and a `memory_ledger` row records the transition.
- **P5 — rule demotion.** A `tier='rule'` row whose effective-precision
  drops below 50% over a 30-day window transitions to `tier='lesson'`,
  `demoted_at` is set, `current_streak` is reset, and a `memory_ledger`
  row records the transition.

**Regression — all 8 from Step 5 still pass:**
- P1 (supersession cascades), P2 (archived excluded), P3 (HIPAA isolation),
  P_cycle, P_archived_mid_cascade, P_stuck_surface_able,
  P_end_session_isolation, P_end_session_idempotent.

### Unit tests

`tests/test_curator_eval_runner.py`:
- Mock LLM responses, assert findings parsed correctly
- Assert second call uses warm cache (input-token-cost reduction)

`tests/test_curator_graduation.py`:
- Each signal handler in isolation
- Threshold edge cases (streak=2 → no graduation, streak=3 → graduation)
- Freshness boundary (last_hit just past 90 days → no graduation)

`tests/test_curator_refinement.py`:
- applies_when widening preserves existing constraints
- Refinement queue cap behavior

### Integration tests

`tests/test_step6_e2e.py`:
- Full state machine flow: QUEUED → PLANNING → IMPLEMENTING → REVIEWING
- Mock LLM eval responses with controlled findings
- Assert tier transitions, ledger rows, refinement queue state

### Coverage gates

- `factory/curator/eval/runner.py` ≥ 85%
- `factory/curator/eval/eval_security.py` ≥ 85%
- `factory/curator/eval/eval_test.py` ≥ 85%
- `factory/curator/graduation.py` ≥ 90% (lots of branching, tight gate)
- `factory/curator/refinement.py` ≥ 85%

---

## 7. Locked design decisions

| # | Decision | Reason |
|---|---|---|
| 1 | Sequential eval calls riding warm cache | ~10x cost reduction on second call; latency 60s acceptable for 2 agents; reversible if we add 5+ later |
| 2 | N=3 consecutive successful preventions, 90-day window | Strict ("consecutive" not "ratio") prevents flickery lessons becoming rules; 90d freshness expires stale lessons |
| 3 | Curator self-introspection for refinement; end_session enrichment as supplemental path | Avoids spawning a separate refinement agent; calling agent can volunteer richer judgment when present |
| 4 | Demotion threshold: precision < 50% over 30-day window | Mirror of graduation strictness; ledger preserves transition history so demotion is reversible |
| 5 | Eval findings persist in `factory_artifacts` (existing Step 5 substrate) | Reuses the fix-loop implementer integration locked in Step 5 |
| 6 | Eval phase runs at IMPLEMENTING → REVIEWING transition | Matches existing factory state machine; fix-loop implementer reads findings during REVIEWING |
| 7 | New columns flat on `devbrain.memory` (current_streak, graduated_at, demoted_at) | No edge semantics; consistent with playbook §9 forward-compat |

---

## 8. Open at implementation time (deferred from design)

Resolve via PR review or while implementing:

- **Eval prompt content** — actual prompt text for eval_security and
  eval_test. Will draft during 6b.
- **Demotion sweep cadence** — every REVIEWING transition is fine for v3.0;
  could move to a periodic task in Phase 6 cognify.
- **applies_when widening heuristic** — v3.0 uses keyword extraction; smarter
  heuristic (semantic similarity) deferred to Phase 3.x.
- **Eval timeout / API-quota handling** — graceful skip for v3.0; adaptive
  backoff for Phase 3.x.
- **`relevant_memory_id` extraction** — eval prompts must be instructed to
  surface which memory in brief their finding maps to (or null if none).
  Prompt-engineering detail.

---

## 9. Implementation order — sub-PRs within Step 6

1. **6a — migration + types + graduation skeleton.** Migration 019 columns
   + index, `factory/curator/eval/types.py`, empty `factory/curator/graduation.py` and `refinement.py` shells.
2. **6b — eval runner + agents.** `factory/curator/eval/runner.py`,
   prompt files, `eval_security.py`, `eval_test.py`. Mock-LLM unit tests.
3. **6c — graduation pipeline.** `factory/curator/graduation.py` filled in,
   three signal handlers, demote sweep. P4 postulate ships.
4. **6d — refinement path.** `factory/curator/refinement.py`, refinement
   queue table (small migration if needed), end_session enrichment hook.
5. **6e — state machine integration + P5 postulate + DB-CI updates.**
   `factory/state_machine.py` `IMPLEMENTING → REVIEWING` hook, P5 postulate
   ships, `.github/workflows/test.yml` `pytest-db` allow-list extended.

After 6e merges, **Atlas Step 6 is done**. Step 7 (rule engine + per-project
compliance profiles + 5 seeded rules) becomes unblocked.

---

## 10. Non-goals (carry forward)

- **Universal rule precision tracking.** Demotion uses a simple ratio over
  a window. Phase 3.x can add smarter precision metrics (per-context, per-
  severity).
- **eval_hipaa / eval_perf / eval_lint.** Step 8+ work. eval_hipaa
  specifically is dissolved into compliance-profile-tagged rules per the
  Step 7 refactor (DevBrain decision `fc1a62bb`).
- **Eval result caching across runs.** Each job re-runs evals against its
  own diff. Caching across diffs is YAGNI.
- **Cross-project rule sharing.** Rules stay project-scoped per Step 4 P3
  postulate. Phase 5 graph layer will give us a cleaner edge-based path.
