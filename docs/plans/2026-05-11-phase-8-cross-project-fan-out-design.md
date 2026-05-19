# Atlas Phase 8 — Cross-Project Fan-Out for Multi-Project Sessions

> **Status:** Design proposal — open questions in §10. Not yet locked.
>
> **Scope:** Add a cognify pass that classifies which projects a session
> actually discussed, then writes focused per-project summary rows into
> each, with `derived_from` edges back to the source session row. Makes
> cross-project session content discoverable from a single-project
> `deep_search` without paying for `cross_project=True` scans.
>
> **Non-goals:** Replacing single-project ownership of source session rows
> (sessions keep one canonical `project_id`); semantic dedup across the
> fan-out targets (Phase 9 if needed); cross-DB fan-out (this is in-DB
> only — laptop devbrain ↔ Mac Studio brightbrain is a transport question,
> not a cognify question).
>
> **Hard prerequisite:** Phase 6's `cognify_extract` must actually run in
> non-dry-run mode with non-zero candidates. As of 2026-05-11, every
> recorded `cognify_extract` run is `dry_run: true, candidate_sessions: 0`.
> Fan-out cannot be built on top of a pass that isn't running. Fix
> `cognify_extract` first.
>
> **Verification gate:** new postulates pass + fan-out CLI runnable +
> deep_search returns breadcrumb edges to source session +
> all 43+ prior postulates still pass.

---

## 1. Drivers

**The real problem this solves:** devs are undisciplined about project
boundaries inside a session. A single session that "is about" brightbot
will spend 15 minutes troubleshooting a devbrain MCP issue, 10 minutes
planning the next pkrelay release, and 90 minutes on the actual brightbot
work. Today, that whole session is owned by one `project_id` (whatever
the agent guessed at end_session time). Searching `project=devbrain` for
"that MCP routing issue we hit" returns nothing — the content is over in
`project=brightbot` and the agent doesn't know to look there.

The current workaround (`cross_project=True` on every `deep_search`) is
expensive — it scans all projects' embeddings every time, defeating the
project-scoping that makes large repos searchable at all.

**Pain points addressed:**

1. **Lost recall when projects are misclassified.** Common today. A 50/50
   session lands in one project; queries against the other find nothing.
2. **`cross_project=True` is too coarse.** It's an "I give up" fallback —
   surfaces noise from unrelated projects, and the agent has to filter.
3. **Pre-project planning sessions orphan content.** Sessions that
   discuss "should I build X" become invisible once project X exists,
   because they were filed under whatever-was-current at the time.
4. **Multi-machine routing aggravates this.** Per the 2026-05-11 audit:
   laptop devbrain and Mac Studio brightbrain each accumulated ~48K
   disjoint `brightbot` rows because sessions on each machine wrote to
   their local `mcp__devbrain__*`. Even after the laptop→Mac Studio
   migration consolidates LHT data, fan-out is the long-term answer
   for multi-project sessions within a single DB.

**Why not "just add a general/inbox project":** a fallback project only
helps sessions that touch *no* specific project. Sessions that touch
*multiple* projects still suffer the "misclassified, content invisible"
problem. Fan-out solves the harder case; the inbox question is a
separate, smaller decision (covered in §6).

## 2. Locked decisions (proposed)

| # | Decision |
|---|---|
| 1 | Source session keeps one canonical `project_id` — fan-out does NOT change session ownership. Fan-out writes ADDITIONAL summary rows in other projects with `derived_from` edges back. |
| 2 | Classifier: LLM call per session at fan-out time. Returns list of `{project_slug, relevance_score, summary_text}`. Cached in `cognify_spend_log` per PR #103 pattern. |
| 3 | Relevance threshold: `score >= 0.30` (LLM-judged "this project was a real subject of discussion, not a passing mention"). Below threshold → no fan-out row in that project. |
| 4 | Fan-out output row shape: `kind='session_summary'`, `tier='memory'`, embedded, `compliance_profiles` inherited from target project (NOT from source) for cross-project rule isolation. |
| 5 | Edge type: `derived_from` (already exists, Phase 5). Edge goes from fan-out summary → source session row. One edge per fan-out row. |
| 6 | New cognify pass: `cognify_fanout`. Runs after `cognify_extract`. Independent schedule via launchd. |
| 7 | Idempotency: a `cognify_fanout` row is keyed by `(source_session_id, target_project_id)`. Re-running with same input is a no-op. Schema-enforced via partial unique index. |
| 8 | Inbox project: deferred. Implement fan-out first; observe whether truly project-less sessions exist as residual; if yes, then add `slug='inbox'` as a fallback. (§6) |
| 9 | Historical reprocess: `devbrain cognify-fanout --since=<date>` for one-time backfill. Default new-only going forward. |
| 10 | Agent semantics: `deep_search(..., with_graph=true)` already surfaces edges (Phase 5). Fan-out leans on existing infrastructure — no new MCP tool required. |

## 3. Architecture

### 3.1 Classifier

```
input:  source session row (project_id, content chunks)
output: [
  {project_slug: "brightbot",  relevance: 0.85, summary: "..."},
  {project_slug: "devbrain",   relevance: 0.40, summary: "..."},
  {project_slug: "pkrelay",    relevance: 0.15, summary: null},  // below threshold
]
```

Single LLM call, warm-prompt-cached (the project taxonomy is stable;
session content varies). Reuses Claude subscription OAuth via PR #119
patterns where available.

**Project taxonomy prompt:** lists active projects from
`devbrain.projects WHERE status='active'` plus their descriptions. LLM
matches session content against the taxonomy. Unrecognized topics →
nothing emitted (or → `inbox` if §6 is enabled).

### 3.2 Output row

For each `relevance >= 0.30` entry, write a memory row:

```sql
INSERT INTO devbrain.memory (
  id, project_id, kind, tier,
  content,              -- the per-project summary from the LLM
  embedding,            -- embedded immediately
  compliance_profiles,  -- inherited from target project
  hit_count, current_streak, ...
) VALUES (...);

INSERT INTO devbrain.memory_dependencies (
  source_id, target_id, edge_type
) VALUES (
  <new_fanout_row_id>,
  <source_session_row_id>,
  'derived_from'
);
```

The `derived_from` edge means: this summary was DERIVED FROM the source
session row. Phase 5's `graph_walk` already returns these edges. Agents
running `deep_search(project='brightbot', with_graph=true)` get the
brightbot fan-out summary in `results[]` AND its `derived_from` edge in
`graph.edges[]`, pointing at the source session in (say)
`project=devbrain`.

### 3.3 Schema

One new constraint on `devbrain.memory_dependencies`:

```sql
-- Idempotency guard for fan-out edges
CREATE UNIQUE INDEX memory_dependencies_fanout_uniq
ON devbrain.memory_dependencies (source_id, target_id, edge_type)
WHERE edge_type = 'derived_from';
```

One optional new column on `devbrain.memory` (deferred to §10 — could
also live in `metadata` JSONB):

```sql
ALTER TABLE devbrain.memory
ADD COLUMN fanout_relevance NUMERIC(3,2);  -- 0.00-1.00, NULL for non-fanout rows
```

Useful for: re-tuning the threshold later without re-running the LLM;
debugging "why did this end up in brightbot?"; sorting fan-out rows
by classifier confidence.

### 3.4 New cognify pass

`factory/cognify/fanout.py` — mirrors the existing 5 passes in
`factory/cognify/`. Entry point:

```
devbrain cognify --pass=fanout [--project=<slug>] [--since=<date>] [--dry-run]
```

Inputs: `kind='session_summary'` rows in any project that don't already
have any `derived_from` edges pointing AT them (= haven't been fanned-out yet).

Output: 0-N new rows in other projects + edges back.

Schedule: launchd `.plist` running every 4h. Cheaper than `cognify_extract`
because it operates on already-summarized sessions, not raw chunks.

## 4. Agent semantics — how this surfaces in retrieval

**Today (without fan-out):**
```
deep_search(project='brightbot', query='MCP routing across machines')
  → 0 results (the conversation was in project=devbrain)
agent: "DevBrain has no fresh context on this."
```

**With fan-out:**
```
deep_search(project='brightbot', query='MCP routing across machines', with_graph=true)
  → results: [{
      id: <fanout_row>,
      content: "Session 2026-05-11 covered MCP routing issue between
                laptop and Mac Studio. Fix: add brightbrain entry to
                ~/.claude.json. Full context in source session.",
      kind: 'session_summary',
      ...
    }]
  → graph.edges: [{
      source: <fanout_row>,
      target: <original_session_in_devbrain_project>,
      edge_type: 'derived_from'
    }]
```

The agent gets:
1. A short, project-scoped summary surfacing in normal `deep_search`.
2. A pointer to the full context via the graph edge.

Agents should then call `get_source_context(source_session_id)` to
read the full raw transcript when needed. This is one extra targeted
call, vs. an expensive cross-project scan.

## 5. Postulates (proposed)

| Postulate | Asserts |
|---|---|
| `P_fanout_idempotent` | Running `cognify_fanout` twice on the same session does NOT produce duplicate rows (unique index enforces). |
| `P_fanout_threshold_floor` | A project mentioned with `relevance < 0.30` does NOT get a fan-out row. |
| `P_fanout_edge_present` | Every fan-out row has exactly one `derived_from` edge pointing at its source session row. |
| `P_fanout_source_isolation` | The source session row's `project_id` is unchanged after fan-out. Fan-out is additive only. |
| `P_fanout_archived_excluded` | Source sessions where `archived_at IS NOT NULL` do not trigger fan-out. |
| `P_fanout_compliance_inheritance` | Fan-out row's `compliance_profiles` matches the TARGET project's profiles, not the source's. Preserves P3 (HIPAA cross-project isolation). |
| `P_fanout_graph_followable` | `deep_search(project=X, with_graph=true)` returning a fan-out row INCLUDES the `derived_from` edge to the source session in `graph.edges`. |
| `P_fanout_supersede_passthrough` | If the source session is superseded (existing edge type), fan-out rows derived from it are flagged stale via the supersedes-walk added in PR #116. |

Adds 8 postulates to the existing 43+ Atlas count.

## 6. The "inbox" project — defer

The case for an `inbox` project: a session that genuinely discusses no
existing project (pure exploration, "should I build X", admin chatter).
Currently these get filed under whatever was-current and get lost.

The case against: real "no-project" sessions are rare. If fan-out's
classifier is honest about low-relevance signals, those sessions just
produce zero fan-out rows and stay where they were filed. That's a
known-bad outcome but a small one.

**Recommendation:** ship fan-out, observe the residual. After ~30 days,
query: of sessions processed by `cognify_fanout`, how many produced
ZERO fan-out rows AND had source `project_id` set to a poor match?
If that number is meaningful (>5% of sessions), add an `inbox` project
and a fallback rule: source `project_id` rewrites to `inbox` when
classifier returns no entries above threshold.

Naming: `inbox` (preferred over `general` — `general` invites a
"throw-everything-here" anti-pattern; `inbox` signals "this needs
sorting / will be revisited").

## 7. Cost model

Per-session cost: 1 LLM classification call + N summary-writing calls
(one per project above threshold). Typical N = 1-2. Warm prompt cache
on the taxonomy keeps the classification call cheap.

Estimate: ~$0.003-$0.008 per session (Haiku for classification, Sonnet
for per-project summary). At 100 sessions/week across all devs, ~$1-3/week.

Spend tracked in `cognify_spend_log` (PR #103). Per-dev attribution via
PR #120's OAuth token plumbing. Budget alarms via `cognify_spend_daily`
view.

## 8. CLI + MCP changes

**New CLI:**
- `devbrain cognify --pass=fanout` — run the pass manually
- `devbrain cognify-fanout --session=<id>` — fan out a single session (debugging)
- `devbrain cognify-fanout --since=<date>` — historical backfill

**No new MCP tools.** Existing `deep_search(with_graph=true)` +
`graph_walk(memory_id)` + `get_source_context(chunk_id)` already cover
the retrieval path. Phase 5's graph layer was designed with exactly this
use case in mind.

**One MCP tool description update:** `deep_search` description should
mention the fan-out pattern explicitly so agents know to set
`with_graph=true` when they get session_summary hits. Reduces the
"agent ignores breadcrumb" failure mode that's already happening
with recency_neighbors (and was the root cause of today's "stale hits"
incident).

## 9. Implementation phasing

| Step | Scope |
|---|---|
| 8a | Fix `cognify_extract` dry-run / 0-candidate issue (HARD PREREQUISITE — Phase 8 cannot ship until cognify_extract is actually producing atomic memories). |
| 8b | `factory/cognify/fanout.py` skeleton + classifier prompt + idempotency unique index + 4 postulates (idempotent, threshold, edge, isolation). |
| 8c | Historical backfill CLI + 4 more postulates (archived_excluded, compliance_inheritance, graph_followable, supersede_passthrough). |
| 8d | launchd `.plist` for scheduled fan-out + ops docs + MCP tool description update prompting agents to use `with_graph=true`. |
| 8e | 30-day observation window. Decide on `inbox` project. |

## 10. Open questions before locking

1. **Threshold value.** `0.30` is a guess. Calibrate against a labeled
   sample of 50 sessions before locking.
2. **Schema decision.** New `fanout_relevance` column or stash in
   `metadata` JSONB? Column is cleaner for filtering/sorting; JSONB
   avoids a migration.
3. **Classification model.** Haiku for cost, or Sonnet for quality?
   Run a quick A/B on 20 sessions and compare.
4. **Per-dev attribution.** Per PR #120, cognify can use the source
   session's dev OAuth. But fan-out happens after end_session — does
   it bill to the original dev or to a system token? Probably system
   (no individual dev is "responsible" for downstream processing) but
   worth a decision.
5. **Cross-DB fan-out** — explicitly out of scope for v1. If/when laptop
   devbrain and Mac Studio brightbrain need to share fan-out, that's a
   separate transport problem (Phase 9?).
6. **Re-extraction interaction.** When `cognify_extract` is re-run on
   a session (per Phase 6's on-demand CLI), should fan-out also re-run?
   Default: yes, idempotency makes it safe. Worth confirming.

---

## Dependencies

- Phase 5 (graph layer + `derived_from` edge type) — ✅ shipped (PRs #97, #101).
- Phase 6 (cognify pass scaffolding) — ✅ shipped (PR #98) but `cognify_extract`
  is broken in production (dry-run only). **8a fixes this.**
- PR #103 (cognify_spend_log) — ✅ shipped.
- PR #116 (recency-neighbor expansion + supersedes auto-walk) — ✅ shipped.
- PR #120 (per-dev OAuth attribution) — ✅ shipped.

## Roll-back plan

If fan-out produces noisy/spammy summaries:

1. Stop the launchd plist (`launchctl unload`).
2. `UPDATE memory SET archived_at=now() WHERE kind='session_summary'
   AND EXISTS (SELECT 1 FROM memory_dependencies WHERE source_id=memory.id
   AND edge_type='derived_from')` — archives ALL fan-out rows. Reversible
   (archive, not delete, per Phase 6 GC policy).
3. No source data is harmed — source sessions and edges are unchanged.

The destination project's normal data is unaffected: fan-out only writes
NEW rows tagged via the `derived_from` edge. Existing rows in any
destination project keep their content, edges, and ranking signals.

---

## 12. Addendum (2026-05-19) — locked specification + build plan

Decisions resolved in the 2026-05-18 working session that turn this
design into a buildable spec. Updates §10's open questions and pins
the classifier shape + schema + PR breakdown.

### 12.1 Locked decisions

| # | Decision |
|---|---|
| A1 | Whole-session relevance threshold: **0.30** (already locked, confirmed today). |
| A2 | Within-section relevance threshold: **0.75** — the classifier identifies topic sections inside the session and only assigns a section to a project when that section is ≥75% about the project. Forces the model to reason section-by-section instead of treating the whole session as a soup. |
| A3 | Per-dev **home project** replaces the deferred §6 inbox. Schema: `slug='home-<dev_id>'`, `name='<Full Name> — home'`, ACL owner = that dev. Created lazily on first end_session that has no clear project signal. **Patrick gets `home-patrickkelly` like every other dev** — no special-case bypass. |
| A4 | Canonical project_id assignment **never moves** for existing rows. Today's `raw_sessions.project_id` (whatever the agent picked at end_session) stays put. Fan-out only ADDS per-project summary rows; it does NOT rewrite source ownership. Rationale: `provenance_id` chains in `memory` already point at the canonical row; reassigning canonical would invalidate atom-to-session lineage for thousands of atoms. |
| A5 | New-session canonical assignment fallback: agent's pick if present, else `home-<dev_id>`. If neither classifier nor agent surfaces any project ≥0.30 → canonical stays `home-<dev_id>`; fan-out emits zero rows. |
| A6 | Source-of-truth row for the `derived_from` edge: edges go from fan-out `memory(kind='session_summary')` → source `memory(kind='session_summary')` row in the canonical project. NOT directly to `raw_sessions` — keeps the graph homogeneous (memory→memory edges, per Phase 5). |
| A7 | Backfill scope: **all-time** (~3,000 raw_sessions today). Sonnet 4.6 classifier with warm-prompt-cached project taxonomy; estimated $15–40 total. Idempotent partial unique index makes re-runs safe. |
| A8 | Per-project focused summary length: 200–800 chars per project entry, model-judged. Long enough to retain useful detail, short enough that 5 fan-out rows from one session stay storage-cheap. |
| A9 | Embedding model: **same `snowflake-arctic-embed2`** as the rest of `memory` — consistent vector space across project searches. |
| A10 | Launchd cadence: `cognify_fanout` runs every **60 min**, gated to only process raw_sessions where `cognify_extract` has already completed (so atoms exist and the session is settled). Independent of `cognify_extract`'s 30-min cadence. |

### 12.2 Classifier prompt + JSON output schema

**Single LLM call per session.** The prompt structure forces section-aware
reasoning even though output is a flat per-project list — the model is
asked to identify sections internally, score per-section, then aggregate.

```
SYSTEM: You are classifying a developer's work session into the projects
        it discussed. Available projects + descriptions:
        {{taxonomy_json}}  — list of {slug, name, description}.

USER: Session content follows. Do this internally:
        (1) Identify topic sections (groups of consecutive turns about
            one subject — could be 1 section or 6, the model decides).
        (2) For each section, score per-project relevance 0.0–1.0.
            A section counts as "belonging" to a project only when its
            within-section score ≥ 0.75.
        (3) Aggregate to session-level per-project relevance = fraction
            of session sections that belong to that project. Drop
            projects below 0.30.
        (4) For each kept project, write a 200–800-char focused summary
            of the relevant sections' content, in the dev's voice.

      Return ONLY JSON (no commentary, no markdown fences):

      {
        "sections": [
          {"start_turn": int, "end_turn": int, "topic": str,
           "project_scores": {"<slug>": 0.0-1.0, ...}}
        ],
        "per_project": [
          {"project_slug": str,
           "session_relevance": 0.0-1.0,    // >= 0.30 to emit
           "section_count": int,             // sections >= 0.75 for this project
           "focused_summary": str}
        ]
      }

      <<< SESSION CONTENT >>>
      {{session_json}}
```

Output validation: drop entries where `session_relevance < 0.30` (defense
in depth — model might emit them anyway). Drop entries whose `project_slug`
isn't in the live taxonomy.

### 12.3 Schema delta (migration 039)

```sql
-- Per-dev home projects. One row per dev_id at backfill time + one row
-- per future dev_id at first cognify_fanout encounter.
INSERT INTO devbrain.projects (id, slug, name, description, created_at)
SELECT gen_random_uuid(),
       'home-' || dev_id,
       COALESCE(full_name, dev_id) || ' — home',
       'Auto-managed catch-all for ' || dev_id || ' sessions with no other clear project. Phase 8.',
       now()
FROM devbrain.devs
WHERE NOT EXISTS (
    SELECT 1 FROM devbrain.projects p WHERE p.slug = 'home-' || devs.dev_id
);

-- Idempotency seal: one fan-out row per (source raw_session, target project).
-- Uses fanout_source_session_id (added below) so the index covers exactly
-- "this fan-out has already written into this project for this source."
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_fanout_unique
    ON devbrain.memory (fanout_source_session_id, project_id)
    WHERE kind = 'session_summary'
      AND tier = 'memory'
      AND archived_at IS NULL
      AND fanout_source_session_id IS NOT NULL;

-- Fan-out attribution column (lets us tell fan-out rows apart from
-- agent-volunteered session_summary rows for the dashboard panel,
-- and powers the idempotency index above).
ALTER TABLE devbrain.memory
    ADD COLUMN IF NOT EXISTS fanout_source_session_id UUID
    REFERENCES devbrain.raw_sessions(id);

COMMENT ON COLUMN devbrain.memory.fanout_source_session_id IS
    'Set on Phase 8 fan-out rows: the raw_session whose content was '
    'classified into this project. NULL for non-fan-out memory rows.';
```

### 12.4 Canonical assignment policy (clarifies §2 decision #1)

```
End-of-session path (live):
  if agent_chose_project: canonical = agent_pick
  else:                   canonical = home-<dev_id>   (lazily created)
  → fan-out runs on the next cognify_fanout tick, regardless of canonical

Existing rows (backfill):
  canonical stays put. Period. Even when classifier disagrees:
    e.g. session is canonical=brightbot, classifier says
    {brightbot: 0.25, devbrain: 0.50, lht-vps: 0.45} — none clear 0.30
    in brightbot, devbrain leads
    → canonical STILL brightbot. Fan-out writes rows in devbrain + lht-vps.
    Original session keeps its provenance chain.

  This trades "perfect classification of historical canonical" for
  "no rewrite of provenance_id pointers on ~2,900 sessions." The cost
  is that historical canonicals occasionally look weird; the alternative
  cost is an additional migration with FK-aware atom-relabeling, which
  is much higher-risk.

Edge case — orphan dev_id:
  Some pre-PR-146 sessions have no associated dev_id (lhtdev, codex
  sessions from early ingest). Backfill assigns them to `home-orphan`
  (created in 039) so they're not lost. Going forward, every adapter
  attaches dev_id at ingest time, so home-orphan stops accumulating.
```

### 12.5 PR breakdown

Three PRs, each with clear "done when" criteria. Sized for ~half-day each.

**PR 1 — Foundation** (closes when):
- Migration 039 applied (home-* projects backfilled, fan-out indexes created, `fanout_source_session_id` column live)
- `factory/cognify/fanout.py` module with `classify_session(session_id, conn) -> list[ClassificationResult]` helper — pure function, no DB writes
- `factory/cognify/fanout_prompt.py` with the prompt template + taxonomy renderer
- Tests: mock-LLM tests for classifier output validation; live-DB tests for migration 039 idempotency
- No fan-out rows written yet; this PR is "the spec exists in code, but nothing emits"

**PR 2 — Pass + CLI**:
- `factory/cognify/fanout.py::run_fanout(conn, project_filter=None, since=None, model=...)` — the writer (calls `classify_session`, inserts fan-out memory rows + `derived_from` edges, records spend in `cognify_spend_log`)
- `bin/devbrain cognify-fanout [--project=X] [--since=YYYY-MM-DD] [--model=...] [--dry-run] [--shard=N/M]` CLI
- `launchd/com.devbrain.cognify-fanout.plist` template (60-min cadence)
- Tests: end-to-end against live DB for a 3-session synthetic fixture; idempotency re-run test; partial-failure resume test
- Done when: `bin/devbrain cognify-fanout --dry-run` reports a plausible scope; `--project=devbrain` writes rows that show up in `deep_search`

**PR 3 — Backfill + verification**:
- One-shot backfill script: `scripts/backfill_fanout_all_time.sh` — shards across N workers, logs progress, supports resume
- Verification postulates: P_fanout_no_canonical_rewrite, P_fanout_idempotent, P_fanout_relevance_threshold_honored, P_fanout_home_project_orphan_handling
- Dashboard panel update: existing SessionsPanel optionally shows "X fan-out rows from this session" link
- Done when: all ~3,000 raw_sessions processed, every postulate passes, ad-hoc `deep_search(project=devbrain, query='brightbot routing')` surfaces relevant cross-project hits with breadcrumbs

### 12.6 Resolutions for §10 open questions

| §10 # | Question | Resolution |
|---|---|---|
| 1 | Threshold value | 0.30 session-level, 0.75 within-section (see A1, A2) |
| 2 | Inbox project | Replaced by per-dev home project (A3) |
| 3 | Backfill scope | All-time (A7) |
| 4 | Classifier model selection | Sonnet 4.6 default; `--model=claude-opus-4-7` for stuck cases (mirrors today's cognify-bulk pattern) |
| 5 | Compliance profile inheritance | Locked at design-doc decision #4 (target project's profiles, not source's) — confirmed |
| 6 | Re-run semantics on partial completion | Partial unique index on `fanout_source_session_id` + per-session classifier checkpoint (A6, 12.3) |

### 12.7 What's still soft

These don't block PR 1 but are worth revisiting after PR 2 lands:

- **Classifier confidence calibration.** The 0.30 / 0.75 thresholds are educated guesses. After 200–500 sessions of real fan-out data, audit a sample manually + adjust if precision/recall is off.
- **Home project promotion.** If a dev's home project accumulates a coherent topic-cluster, the right move might be promoting that into a real project. Out of scope for Phase 8; revisit in Phase 9.
- **Per-section retention in fan-out output.** Today fan-out emits ONE row per (session, target_project). If sections turn out to be conceptually distinct enough to warrant separate rows, we can split into per-section fan-out — but that doubles storage and complicates `derived_from` graph queries. Defer.
