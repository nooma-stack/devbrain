# Atlas / Phase 3 — Session Continuation Playbook

> **Purpose:** Handoff document for resuming work on Phase 3 / Atlas Steps 5-7
> after the 2026-04-29→04-30 "post-compaction continuation" sprint paused at
> Step 4. Locks in two design refinements made in conversation on 2026-05-02
> and resequences the remaining work.
>
> **References:**
> - PR #67 (`docs/phase-3-discipline-atlas`) — original design doc, still OPEN
> - PRs #68, #69, #70, #71 — Atlas Steps 1-4, MERGED
> - DevBrain decisions: "Atlas Step 5/6/7 (PAUSED)" entries from 2026-04-30

---

## 1. Status snapshot

| Step | What it ships | Status | Reference |
|---|---|---|---|
| 1 | `memory_dependencies` edge table + `store` tool `depends_on`/`supersedes` | MERGED | #68 |
| 2 | `memory_ledger` (hash-chained audit) + AFTER triggers | MERGED | #69 |
| 3 | `verify_chain()` SQL fn + `devbrain audit verify` CLI | MERGED | #70 |
| 4 | First three postulate tests (P1, P2, P3) | MERGED | #71 |
| 5 | **Curator agent + cascade re-evaluation queue** | NOT STARTED | this doc |
| 6 | **eval_security + eval_test + lesson graduation pipeline** | NOT STARTED | this doc |
| 7 | **Compliance rule engine + per-project profiles + seeded rules** | NOT STARTED | this doc |

**Substrate verified in place** for Steps 5-7:

- `devbrain.memory.tier` column with CHECK `IN ('memory','lesson','rule')`
- `devbrain.memory.applies_when` JSONB
- `devbrain.memory_dependencies` edge table
- `devbrain.memory_ledger` hash chain + `verify_chain()`
- `factory_artifacts` table (eval finding shape)
- `tests/postulates/` harness with strict-xfail pattern
- P1 + P2 postulates already xfail-strict — they flip green automatically when
  Step 5 ships; that's the regression net.

**Gate points still open:**

- **PR #67** is the only open Atlas PR. It's a doc-only PR. Decision: merge
  it once Step 5 lands so the design contract and the first implementation are
  introduced together. Until then, this playbook is the working spec.
- The 2026-04-30 "another session is working on it" warning has produced no
  visible artifacts in 3 days (no PRs, branches, comments, or commits).
  Treating as resolved unless Patrick says otherwise.

---

## 2. Locked decisions (this session, 2026-05-02)

### 2.1 Compliance is per-project profiles, not BrightBrain-special

Drop the BrightBrain-instance-specific HIPAA bake-in. Compliance is a
configurable per-project capability:

- A `compliance_profile` is a named bundle (`hipaa`, `soc2`, `ferpa`, etc.).
- Rule rows declare which profiles they belong to.
- Projects declare which profiles are enabled.
- At job startup, the curator loads the union of rules whose profiles intersect
  the project's enabled profiles.

This **simplifies** the original Step 7 scope (no special instance handling)
and dissolves the planned `eval_hipaa` agent into data — HIPAA becomes a tag
on rule rows, not a domain agent.

### 2.2 Storage shape — Option A: `compliance_profiles text[]` on `devbrain.memory`

Three options were considered. **Locked: Option A.**

- **A) `compliance_profiles text[]` column on `devbrain.memory` + GIN index.**
  Denormalized, fast filter on the curator's hot path, profile count per rule
  is small. **Chosen.**
- B) Nested in `applies_when` JSONB. Zero migration but slower filter.
- C) Separate `rule_compliance_profiles` join table. Cleanest normalization
  but adds a join on the curator hot path.

Project-side enablement: a new `compliance_profiles_enabled text[]` column on
`devbrain.projects` (or a single column on a future `project_settings` table —
TBD at implementation time).

### 2.3 Atlas vs. existing Phase 3 boundary

Documented for future-you and any reviewer:

- **Atlas-inspired changes** are memory-integrity properties only:
  `memory_dependencies` (Steps 1, 5), `memory_ledger` + `verify_chain` (Steps
  2, 3), postulate harness (Step 4).
- **Steps 6 (eval agents) and 7 (rule engine)** are the *original* Phase 3
  plan that pre-dates Atlas. Atlas plugs into them via the postulate harness
  and audit ledger but did not introduce them.

Stop calling Steps 6-7 "Atlas Steps" — they're "Phase 3 Steps." This avoids
confusion when explaining what Atlas borrowed and what was already Phase 3.

---

## 3. Step 5 — Curator agent + cascade re-evaluation queue

### What ships

A curator process that, per factory job, ranks/promotes/demotes memories and
produces the brief that subsequent agents (Step 6 evals, eventually fix-loop
implementer) consume. Also: a cascade re-evaluation queue so that when a
memory row is invalidated or superseded, every dependent row is re-scored.

### Why it unblocks everything else

- Flips P1 (supersession cascades) and P2 (archived excluded) from
  `xfail(strict=True)` to passing.
- Produces the brief that Step 6 eval agents consume.
- Produces the in-brief / not-in-brief signals that drive lesson graduation
  (Step 6) and rule promotion (Step 7).

### Open design questions (resolve at design time)

1. **Tick cadence + cost ceiling.** Per-job (curator runs once per factory
   job kickoff), per-tick (timer-based), or both? What's the LLM-call budget
   for a single brief?
2. **Cascade-signal weighting.** When a memory's dependency changes, does
   that affect strength multiplicatively or additively in the existing Enso
   formula? Default proposal: additive penalty proportional to time since the
   dependency change, capped.
3. **Brief shape.** What does the curator output look like to a downstream
   agent? Likely structured: `{lessons: [...], rules: [...], context: ...}`.
   Implementers should produce a Pydantic model in `factory/curator/types.py`
   so all consumers see the same shape.

### Verification gate

P1 + P2 postulate tests must flip from xfail to pass. New unit tests for the
re-evaluation queue (queue-empty, single-cascade, multi-hop cascade,
self-referential safety).

---

## 4. Step 6 — eval_security + eval_test + lesson graduation pipeline

### What ships

Two domain-specialized agents that run AFTER the implementer (in parallel
with each other), sharing the curator-warmed cached context:

- `eval_security` — auth, injection, secret leakage, dependency CVE check.
- `eval_test` — coverage of the diff, test quality, brittleness flags.

Findings flow into `factory_artifacts` rows as JSON:
`{rule_id, severity, file, line, message, fix_hint}`. The fix-loop implementer
reads them as actionable items (matches the locked 2026-04-15 "directed after"
implementer asymmetry).

The lesson graduation pipeline goes live in this step. Three feedback signals
(locked decision):

1. *In-brief AND failure happened* → `hit_count++`. Repeat → graduation candidate.
2. *NOT in-brief but should have been* → curator ranking failed. Refinement
   path proposes `applies_when` updates.
3. *In-brief AND code correct first pass* → `effective_hit_count++`, strength
   reinforced via Enso formula.

### Open design questions (resolve at design time)

1. **Agent parallelism mechanism.** Actual parallel processes/threads, or
   sequential calls riding the same warm prompt cache? Default proposal:
   start sequential (simpler, prompt-cache-friendly), measure latency,
   parallelize if it matters.
2. **Graduation threshold N.** N=3 fixed? Configurable per-project?
   Time-bounded ("3 hits in 30 days")? Default proposal: N=3, configurable
   per-project, time-bounded to last 90 days.
3. **Refinement path for signal #2.** Separate refinement agent, or curator
   self-introspection at the end of each tick? Default proposal: curator
   self-introspection — fewer moving parts.

### Verification gate

New postulate tests:
- P4: a lesson included in N consecutive briefs followed by N successful
  preventions becomes a graduation candidate.
- P5: an `eval_*` rule firing with low precision over a window is demoted
  back to `lesson` tier.

---

## 5. Step 7 — Compliance rule engine + per-project profiles + seeded rules

### What ships

The `tier='rule'` slice of `devbrain.memory` becomes the rule engine's
runtime input. Two execution modes coexist (unchanged from PR #67):

1. **Agent-based (default).** An `eval_*` agent reads relevant rules and
   applies them via prompt. Suits fuzzy/semantic rules.
2. **Declarative JSON predicate.** Explicit rule rows with `predicate`
   (regex / AST pattern / SQL query), `severity`, `applies_when`. Engine
   evaluates programmatically. Suits hard requirements.

**New in this step (per 2.1 + 2.2 above):**

- Migration adds `compliance_profiles text[]` to `devbrain.memory` with GIN
  index.
- Migration adds `compliance_profiles_enabled text[]` to `devbrain.projects`.
- Curator filters rules by `rule.compliance_profiles ∩ project.compliance_profiles_enabled`.
- Five seeded rules across multiple profiles (NOT just HIPAA):
  - 2-3 HIPAA rules (PHI logging, audit-log completeness, etc.)
  - 1 SOC2 baseline rule (e.g., secret-leakage detection)
  - 1 FERPA rule (relevant for LHT school-data projects)

Each seeded rule MUST ship with at least one postulate test in
`tests/postulates/test_pN_<rule_slug>.py` proving the rule prevents what it
claims to prevent. CI lint check: any rule row with non-empty
`compliance_profiles` without a matching postulate is a CI failure.

### Open design questions

1. **Predicate language per rule kind.** Pick one representation per kind
   (e.g., AST for code-pattern rules, SQL for table-policy rules) and
   document the contract.
2. **CI lint check location.** GitHub Actions workflow that greps for
   profile-tagged rules without matching tests, or a `devbrain rules lint`
   CLI subcommand? Default proposal: CLI subcommand invoked from CI — also
   usable locally.
3. **Profile naming + namespace.** Free-form strings (`"hipaa"`), or
   enum-checked? Default proposal: free-form, but reserved names list in
   `config/compliance_profiles.yaml` as advisory.
4. **Project enablement governance.** Who can edit a project's
   `compliance_profiles_enabled`? Audit ledger records the change but doesn't
   gate. Probably fine for v3.0 — revisit if needed.

### Verification gate

- Per-rule postulate tests pass.
- New postulate P6: a project with `compliance_profiles_enabled = []` sees
  zero rules applied.
- New postulate P7: a project with `compliance_profiles_enabled = ['hipaa']`
  sees only HIPAA-tagged rules applied (not FERPA or SOC2).

---

## 6. Implementation order

Each step is its own PR(s). No big-bang.

1. **Step 5: curator agent.** Smallest viable curator that produces a brief
   and runs the cascade re-evaluation queue. P1 + P2 flip green.
2. **Step 6: eval_security + eval_test + graduation pipeline.** Two agents
   wired to the curator's brief, three feedback signals tracked, P4 + P5
   postulates added.
3. **Step 7: rule engine + compliance profiles + seeded rules.**
   - 7a: migration for `compliance_profiles` columns + GIN index +
     postulates P6 + P7.
   - 7b: curator filter logic + `devbrain rules lint` CLI + CI hookup.
   - 7c: five seeded rules across HIPAA/SOC2/FERPA, each with its own
     postulate.

After 7c lands, merge PR #67 (the design doc) so the contract and the
implementation are introduced together. Then close out Phase 3 v3.0.

Steps 8+ (more eval agents — `eval_perf`, `eval_lint`; rule refinement agent;
demotion automation) are Phase 3.x increments. Out of v3.0 scope.

---

## 7. Coordination notes

- **Register the active session** in `~/.claude/shared-state/WORK_LOG.md`
  before landing the first Step 5 commit. The 2026-04-30 session got burned
  for skipping this; don't repeat it.
- **PR #67 stays OPEN** until Step 7c lands. It's the design contract; the
  implementation PRs reference it.
- **The 2026-04-30 "other session is working on it" signal is treated as
  resolved** unless Patrick says otherwise. No artifacts surfaced in 3 days
  (no PRs, branches, commits, or comments referencing curator/eval/rule
  work).
- **mcp-server already rebuilt** for Steps 1-4 store-tool params (per
  WORK_LOG entry from 2026-04-30). No additional rebuild needed until Step 5
  introduces new MCP surface (e.g., a `curator_brief` tool — TBD at
  implementation time).

---

## 8. Non-goals (carry forward from PR #67 §10)

- Universal supersession metadata. Backfill happens lazily per project as
  needed; Step 1 already wired the supersedes edge type for new writes.
- AGM postulate strict compliance. DevBrain is not a belief-revision
  research project. Methodology + one mechanism borrowed, not formal AGM
  compliance.
- BrightBrain-instance-specific anything. Removed by 2.1 above.

---

## 9. After Phase 3 — what's next on the roadmap

Phase 3 is **not the endpoint.** The full DevBrain roadmap (per
`README.md` §"What's not in v0.1" and `ARCHITECTURE.md` §9) has two more
substantial phases after Phase 3 lands. Listed here so design decisions in
Steps 5-7 don't foreclose options later.

### Phase 5 — Graph layer (Apache AGE + `memory_edges`)

**Inspiration:** OpenBrain (knowledge graphs in pgvector) and Atlas (Neo4j
belief network). DevBrain's twist: stay single-DB by using **Apache AGE**
(Postgres extension that adds Cypher-style graph queries).

**What it ships:** A `memory_edges` table that generalizes the
`memory_dependencies` table from Step 1. Adds edge types beyond
`depends_on` / `supersedes` (e.g., `derived_from`, `contradicts`,
`refined_by`). Multi-hop retrieval ("lessons that supersede rules that
affect file X") becomes a Cypher traversal instead of N recursive SQL
CTEs.

**What carries forward from Phase 3:**
- `memory_dependencies` from Step 1 is the seed table — Phase 5 generalizes
  its shape, doesn't replace it. Don't change `memory_dependencies` in
  ways that make the generalization painful (e.g., avoid baking
  edge-type-specific columns; keep the type as a `text` column).
- `memory_ledger` from Step 2 should already record edge mutations.
  Verify this when Step 5 ships any cascade-driven edge writes.

### Phase 6 — Cognify / Memify pipeline split

**Inspiration:** Cognee (the third memory framework, separate from
OpenBrain and Atlas). Cognee names this split.

**What it ships:** Splits the ingest pipeline:
- **Memify** — raw session/transcript intake. Cheap, always-on, lossless.
- **Cognify** — periodic background pass that extracts structured
  knowledge from the raw store, reweights, decays, promotes. Expensive,
  async, schedulable.

Today DevBrain does both inline at ingest time. Phase 6 splits them so
cognify can run on its own schedule and self-improve without re-ingesting.

**What carries forward from Phase 3:**
- The **curator agent (Step 5)** is conceptually the first cognify
  consumer — it reads the structured memory store and produces a brief.
  Phase 6 will let cognify *write* into that store on a schedule, refining
  what the curator reads.
- The **lesson graduation pipeline (Step 6)** is also a cognify-style
  process (offline reweighting via the three feedback signals). Phase 6
  may absorb it into a unified cognify pass instead of running it
  per-job.
- The **rule engine (Step 7)** should treat its rules as durable
  artifacts that survive cognify reweighting — i.e., cognify should not
  garbage-collect `tier='rule'` rows even if their `hit_count` is low.

### Implication for this playbook's design choices

Steps 5-7 should prefer **shapes that generalize** over shapes that are
locally optimal but boxed in:

- Brief shape (Step 5) — keep it a versioned Pydantic model so cognify
  can produce a richer brief later without breaking consumers.
- `applies_when` JSONB and `compliance_profiles text[]` (Steps 6-7) —
  intentionally flat, no nested edge semantics. Edge semantics belong in
  `memory_edges` (Phase 5), not on the row.
- Curator's strength formula — keep it a pure function of inputs that
  cognify can also call offline. Avoid mutable-state-only-curator-knows
  shortcuts.

### Phases 7-8

Not enumerated in the README. Operational polish + cross-platform
implied. Out of scope for any current planning until Phase 6 ships and
the operational picture is clearer.
