# Atlas Phase 6 — Cognify / Memify Pipeline Split

> **Status:** Design locked, ready for implementation plan.
>
> **Scope:** Formalize the boundary between raw intake (memify) and structured
> knowledge extraction (cognify). Add scheduled background cognify passes
> that run on their own cadences, independent of session ingest events.
>
> **Non-goals:** Re-extraction with versioning + supersession edges (Phase 7
> when extraction-quality-evolution is a real signal); full spend tracking
> infrastructure (Phase 7 ops); event sourcing rewrite of ingest (we're
> formalizing what exists, not replacing it).
>
> **Verification gate:** new postulates pass + 5 cognify passes runnable via
> CLI and via launchd `.plist` files + all 22+7 prior postulates still pass.

---

## 1. Drivers

**Why split memify from cognify:**

1. **Cognify is expensive.** Every session ingest today triggers extraction
   even on dormant projects. Cognify-flavored work should run on its own
   cadence — hot for sessions that produce learnable patterns, cold for
   sessions that are just record-keeping.

2. **Cognify can't self-improve today.** Once a session is processed, we
   never revisit it with better extraction prompts or upgraded models.
   Phase 6 lays the structural ground for re-extraction (full version-
   tracked re-extraction is Phase 7; v1 ships an on-demand CLI escape
   hatch).

3. **Decay and GC don't run.** Memory rows accumulate. `hit_count` only
   decays on triggering events. Long-tail HIPAA artifacts sit stale.
   Phase 6 ships a `cognify_decay` pass that runs on schedule
   regardless of activity.

4. **Cross-session insights are missed.** Extraction is per-session today.
   Patterns spanning multiple sessions never surface unless something
   else triggers a multi-session pass. Phase 6 gives `cognify_extract`
   a multi-session input mode.

## 2. Locked decisions

| # | Decision |
|---|---|
| 1 | Execution model: **several specialized cognify passes**, each independently scheduled. NOT a single monolithic pass; NOT event-driven only |
| 2 | Scheduler: **macOS launchd** with one `.plist` per pass. CLI command (`devbrain cognify --pass=<name>`) wraps each pass for ops + manual invocation |
| 3 | Module boundary: keep `ingest/` (= memify) as-is; add `factory/cognify/` for scheduled passes; move `factory/curator/graduation.py` + extraction logic out of `factory/curator/end_session.py` into `factory/cognify/`; brief generator + types stay in `factory/curator/` (consumer, not pass) |
| 4 | Curator ↔ cognify coordination: cognify writes always additive; existing `memory_ledger` AFTER trigger captures changes; no snapshot isolation needed |
| 5 | Contradiction detector: LLM-driven for v1; regex augmentation deferred |
| 6 | GC policy: **archive only** (set `archived_at`). NEVER DELETE. HIPAA + audit trail required |
| 7 | Pass granularity: **5 specialized passes** — `cognify_extract`, `cognify_decay`, `cognify_edges`, `cognify_strengthen`, `cognify_gc` |
| 8 | Cost controls: per-pass LLM call max + warm prompt cache for v1; full spend tracking deferred to Phase 7 |
| 9 | Re-extraction policy: **C — on-demand via CLI** (`devbrain cognify-reextract --session=<id>` or `--all`). No automatic version-bumped re-extract for v1. Versioned re-extraction is Phase 7 |
| 10 | Phase 5 link: `cognify_edges` uses `graph_walk` (Phase 5) to find candidate contradiction pairs efficiently — pairs within K hops are higher-priority than random pairs |

## 3. Module boundary

### Memify (raw intake) — unchanged

`ingest/` directory — already implements memify:
- `pipeline.py` — file → adapter → session → chunks → embeddings → memory rows
- `chunker.py`, `embeddings.py`, `summarize.py`, `memory_writer.py`
- Adapters in `ingest/adapters/` (Claude Code, Codex, Gemini, OpenClaw, Markdown)
- `codebase_indexer.py` for source-tree ingest

Phase 6 does NOT rename `ingest/` → `memify/`. The directory's purpose is
clear from the existing module names; cosmetic rename adds churn without
clarification value.

**What moves OUT of `ingest/`:** nothing. Memify stays exactly as it is.

### Cognify (structured extraction) — new

`factory/cognify/` directory — new home for the 5 scheduled passes.

What moves IN from `factory/curator/`:
- Lesson extraction logic from `factory/curator/end_session.py` →
  `factory/cognify/extract.py`
- Graduation pipeline from `factory/curator/graduation.py` →
  `factory/cognify/strengthen.py`
- Refinement pipeline from `factory/curator/refinement.py` →
  `factory/cognify/refine.py` (folded into the broader cognify model;
  refinement is a cognify pass, not a curator-private operation)

What stays in `factory/curator/`:
- `brief.py` — the brief generator (curator's *read* path, consumes cognify output)
- `types.py` — Pydantic models shared between curator and cognify
- `worker.py` — cascade re-eval queue drainer (this stays a curator concern;
  it's read-side reaction to memory mutations)
- `end_session.py` — keeps the orchestration shell, but delegates
  extraction work to `factory/cognify/extract.py`

### New: `factory/cognify/`

```
factory/cognify/
├── __init__.py
├── orchestrator.py         # `devbrain cognify --pass=<name>` entrypoint
├── extract.py              # cognify_extract: lesson/decision extraction
├── decay.py                # cognify_decay: time-based strength decay
├── edges.py                # cognify_edges: auto-infer cites/contradicts
├── strengthen.py           # cognify_strengthen: lesson graduation
├── gc.py                   # cognify_gc: archive low-strength orphans
└── reextract_cli.py        # `devbrain cognify-reextract --session=<id|all>`
```

## 4. The 5 cognify passes

### 4.1 `cognify_extract` — lesson + decision extraction

**Purpose:** read recently ingested raw chunks; produce structured lessons
and decisions; populate `derived_from` edges (Phase 5) pointing back to
source sessions.

**Cadence:** hourly (catches sessions ingested in the last hour).

**Cost ceiling:** max 20 LLM calls per pass. Sonnet 4.6 with prompt
caching. Cumulative pass-time budget: 5 minutes.

**Input:** sessions ingested since last successful pass (tracked via
`devbrain.cognify_run_log` — new table, see §7).

**Output:** new `devbrain.memory` rows of `kind in (decision, lesson)`
with `derived_from` edges to the source session chunks.

**Idempotency:** `(provenance_id, kind)` unique-ish constraint. Re-running
on the same session is a no-op unless content changed.

### 4.2 `cognify_decay` — strength decay

**Purpose:** apply time-based exponential decay to memory `strength`
columns. Memory unused for 30 days drops 50%; unused for 90 days drops
to ~12%.

**Cadence:** hourly (cheap arithmetic; no LLM cost).

**Cost ceiling:** zero LLM. SQL-only.

**Implementation:** single UPDATE statement using `last_cascade_at` and
`hit_count_updated_at` to decide the decay multiplier. Pure function,
deterministic — runs in ms.

**Output:** updated `strength` values; ledger row per change.

### 4.3 `cognify_edges` — auto-infer edges

**Purpose:** detect `cites` (narrative mentions) and `contradicts`
(semantic conflicts) edges between memory rows. Populate `memory_dependencies`.

**Cadence:** every 6 hours.

**Cost ceiling:** max 15 LLM calls per pass for `contradicts` detection
(hardest); regex/text-match for `cites` (zero LLM cost).

**Implementation:**
- `cites`: regex over memory content for cross-references. Cheap, deterministic.
- `contradicts`: LLM-judged comparison of pairs. Pair selection uses Phase 5's
  `graph_walk` to find candidates within 3 hops of each other (more likely
  to be related, so worth comparing) instead of comparing all-pairs (N²).

**Phase 5 dependency:** This pass requires Phase 5 to be live. If Phase 5 is
not yet shipped, `cognify_edges` reverts to all-pairs comparison within a
project (capped to recent N rows). Bounded fallback.

### 4.4 `cognify_strengthen` — lesson graduation

**Purpose:** the existing graduation pipeline from `factory/curator/graduation.py`,
moved here. Promotes lessons to rules at N=3 consecutive successful
preventions in 90-day window. Demotes rules below 50% precision over 30-day window.

**Cadence:** daily.

**Cost ceiling:** zero LLM (uses existing precision tracking).

**Implementation:** unchanged from Step 6c. Move the file, update imports,
add it as a launchd `.plist`.

### 4.5 `cognify_gc` — archive low-strength orphans

**Purpose:** archive (set `archived_at`) memory rows whose `strength` has
decayed below threshold AND have zero outgoing edges (orphan).

**Cadence:** weekly.

**Cost ceiling:** zero LLM. SQL-only.

**Threshold:** `strength < 0.1 AND last_cascade_at < NOW() - INTERVAL '90 days'
AND no outgoing dependents`.

**Output:** sets `archived_at` on matching rows; ledger row per archive.
Never deletes.

## 5. Schedule + invocation

### CLI entrypoint

```
devbrain cognify --pass=<extract|decay|edges|strengthen|gc>
devbrain cognify --all                    # runs all passes in dependency order
devbrain cognify --dry-run --pass=<name>  # report what it would do, do nothing
devbrain cognify-reextract --session=<id>  # on-demand re-extract
devbrain cognify-reextract --all          # re-extract all sessions for a project
```

### launchd plists

`~/Library/LaunchAgents/com.devbrain.cognify-<pass>.plist` per pass.
Provided as templates in `factory/cognify/launchd/`. `devbrain setup`
installs them with placeholders filled in.

Example `com.devbrain.cognify-decay.plist` runs hourly:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>com.devbrain.cognify-decay</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/devbrain</string>
    <string>cognify</string>
    <string>--pass=decay</string>
  </array>
  <key>StartInterval</key><integer>3600</integer>
  <key>StandardOutPath</key><string>~/.devbrain/logs/cognify-decay.log</string>
  <key>StandardErrorPath</key><string>~/.devbrain/logs/cognify-decay.err</string>
</dict></plist>
```

### Schedule summary

| Pass | Cadence | Trigger | LLM cost |
|---|---|---|---|
| `cognify_decay` | hourly | launchd | 0 |
| `cognify_extract` | hourly | launchd | up to 20 calls/pass |
| `cognify_edges` | every 6h | launchd | up to 15 calls/pass |
| `cognify_strengthen` | daily | launchd | 0 |
| `cognify_gc` | weekly | launchd | 0 |

**Linux/CI fallback:** `cron` instead of launchd; same CLI commands. Phase 6
ships both `.plist` templates and a `crontab` template.

## 6. Re-extraction policy

**Phase 6 ships C: on-demand re-extract via CLI.**

```
devbrain cognify-reextract --session=<id>  # one session
devbrain cognify-reextract --all           # all sessions in current project
devbrain cognify-reextract --since=<date>  # sessions ingested since date
```

When invoked, archives existing extracted lessons/decisions for the target
session (sets `archived_at`), runs `cognify_extract` against fresh content
with the current extraction prompt, writes new rows.

**Why C and not B (versioned auto-re-extract):**
- B requires extraction_version tracking, supersedes-edge accumulation,
  backfill cost analysis. Real complexity.
- We don't yet have a signal showing extraction quality evolves enough
  to justify periodic re-extraction. After 3-6 months of operation we'll
  know — that's when B becomes the right next step (Phase 7).
- C costs a CLI flag + a switch in `cognify_extract` — minimal scope expansion.

**Audit trail:** re-extraction archives prior rows (not deletes), so the
memory_ledger captures the full history. New rows carry
`metadata.reextracted_from = <prior_row_id>` for traceability.

## 7. Coordination with curator + Phase 5

### Curator (Phase 3 Step 5) consumption

The brief generator (`factory/curator/brief.py`) reads cognify output. After
Phase 6 lands:
- Brief generator's `_load_lessons` returns more lessons (cognify_extract has
  been re-extracting on its own cadence, so the corpus is richer than today).
- Brief generator's `_load_decisions_matching` finds more decisions for the
  same reason.
- `_load_recent_cascade_signals` continues to read from `memory.last_cascade_at`,
  which cognify writes update via the existing AFTER trigger.

**No code changes required in the brief generator** — it already reads
generic memory rows; cognify just writes more of them.

### Phase 5 (graph layer) consumption

`cognify_edges` calls `factory.graph.walker.walk()` to find candidate
contradiction pairs. Without Phase 5 it falls back to all-pairs comparison.

`cognify_extract` writes `derived_from` edges via `memory_dependencies`
INSERT — uses Phase 5's relaxed CHECK constraint (Phase 5a migration 024).

### New table: `devbrain.cognify_run_log`

Tracks per-pass runs for idempotency + observability.

```sql
CREATE TABLE devbrain.cognify_run_log (
    id              BIGSERIAL PRIMARY KEY,
    pass_name       TEXT NOT NULL,                -- 'extract', 'decay', etc.
    project_id      UUID REFERENCES devbrain.projects(id),
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    rows_processed  INTEGER,
    llm_calls       INTEGER DEFAULT 0,
    error           TEXT,
    metadata        JSONB
);
CREATE INDEX idx_cognify_run_log_pass_started
    ON devbrain.cognify_run_log (pass_name, started_at DESC);
```

`cognify_extract` reads this to find "sessions ingested since last successful
pass" — the `started_at` of the most recent successful run.

## 8. Postulates + verification

**New postulates** (under `tests/postulates/`):

| Postulate | Asserts |
|---|---|
| `P_cognify_idempotent_extract` | Running `cognify_extract` twice on the same session produces no duplicate rows |
| `P_cognify_decay_monotonic` | Decay only decreases `strength`; never increases |
| `P_cognify_gc_archive_only` | `cognify_gc` sets `archived_at`; never DELETEs |
| `P_cognify_no_phi_in_logs` | `cognify_run_log.metadata` and stdout never contain raw PHI from memory rows (uses HIPAA seeded rule pattern from Phase 7) |
| `P_cognify_run_log_isolation` | `cognify_run_log` rows are project-scoped; cross-project queries don't leak |
| `P_cognify_reextract_archives_prior` | `cognify-reextract` archives prior rows (sets `archived_at`); doesn't delete; new rows carry `reextracted_from` metadata |
| `P_cognify_strengthen_unchanged` | The graduation/demotion semantics from Step 6c are preserved bit-for-bit after the file move |

**Existing tests must still pass:** all 22+7 prior postulates (Phase 5
adds 7), 5 curator-brief tests, 5 per-rule postulates, `devbrain rules
lint`, factory cascade tests.

**Integration tests:**
- `factory/tests/test_cognify_extract.py` — extraction correctness + idempotency
- `factory/tests/test_cognify_decay.py` — decay arithmetic
- `factory/tests/test_cognify_edges.py` — cites + contradicts inference (mocked LLM)
- `factory/tests/test_cognify_strengthen.py` — graduation moved cleanly
- `factory/tests/test_cognify_gc.py` — archive selection
- `factory/tests/test_cognify_reextract.py` — on-demand re-extract

**Coverage gate:** each new module ≥ 85%.

## 9. Sub-PR sequence

```
6a (substrate + run log)        →  6b (cognify_decay + cognify_gc)
                                →  6c (cognify_extract + extract move)
                                →  6d (cognify_edges + cognify_strengthen)
                                →  6e (launchd plists + reextract CLI = Phase 6 closeout)
```

Sequential merge on CI green.

| Sub-PR | Title | Scope |
|---|---|---|
| 6a | `feat(cognify): Atlas Phase 6a — cognify substrate + run log` | New `factory/cognify/` skeleton, `cognify_run_log` migration, base orchestrator class, `devbrain cognify --pass` CLI scaffolding, 2 substrate postulates |
| 6b | `feat(cognify): Atlas Phase 6b — cognify_decay + cognify_gc passes` | Two SQL-only passes; cheapest to ship first as a confidence check; 2 postulates |
| 6c | `feat(cognify): Atlas Phase 6c — cognify_extract pass + module move` | Move extraction logic out of `factory/curator/end_session.py` into `factory/cognify/extract.py`; ship hourly extract pass; 1 postulate + 1 integration test |
| 6d | `feat(cognify): Atlas Phase 6d — cognify_edges + cognify_strengthen passes` | Auto-edge inference + graduation move; depends on Phase 5 graph_walk (or fallback); 2 postulates |
| 6e | `feat(cognify): Atlas Phase 6e — launchd plists + reextract CLI (Phase 6 done)` | All `.plist` templates, `devbrain cognify-reextract` CLI, ops docs, 1 closeout postulate, milestone store |

## 10. Out of scope (carried forward)

- **Versioned re-extraction with supersession edges** — Phase 7 once
  extraction-quality-evolution becomes a real signal
- **Full LLM spend tracking** — Phase 7 ops (per-project per-day budget,
  alerts, rate limit response handling)
- **Cognify dashboard / observability UI** — Phase 7 ops
- **Cross-project cognify** (e.g., "find contradictions across all enabled
  HIPAA projects") — Phase 7+ once the use case is concrete
- **Real-time cognify (streaming)** — Phase 6 is batch only; streaming
  cognify is a much bigger architecture decision
- **`ingest/` rename to `memify/`** — cosmetic; keep current name

## 11. Forward compatibility

- **Phase 5 substrate is reused, not modified.** `cognify_edges` is a writer
  to `memory_dependencies`; Phase 5 walker reads it. No schema changes from
  Phase 6 affect Phase 5.
- **Phase 7 versioned re-extraction** is enabled by Phase 6's `metadata`
  conventions on lessons/decisions (specifically `reextracted_from`).
  Adding `extraction_version` is one column ALTER away.
- **Phase 7 spend tracking** layers on top of `cognify_run_log.llm_calls`;
  sum-by-project-day is a SQL view.
- **Curator agent (Step 5)** continues to be a read-side consumer. Phase 6
  doesn't change the brief shape, so curator code is unchanged.

## 12. References

- `docs/plans/2026-05-02-session-continuation-playbook.md` §9 — original Phase 6 sketch
- `docs/plans/2026-05-05-phase-5-graph-layer-design.md` — Phase 5 design (prerequisite for `cognify_edges` efficient pair selection)
- DevBrain decision `9287ab95` — Atlas Phase 3 complete milestone
- Migration 014, 022, 024 — memory_dependencies, compliance_profiles, edge type expansion
- `factory/curator/end_session.py` — current extraction logic (moves to `factory/cognify/extract.py`)
- `factory/curator/graduation.py` — current graduation logic (moves to `factory/cognify/strengthen.py`)
