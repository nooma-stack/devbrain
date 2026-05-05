# Atlas Step 7 — Rule Engine + Per-Project Compliance Profiles + 5 Seeded Rules

> **Combined design + implementation plan** (Step 7 is small enough that splitting design and plan adds friction). Most decisions are locked from earlier work.
>
> **Status:** Ready for implementation.
>
> **References:**
> - `docs/plans/2026-05-02-session-continuation-playbook.md` §5 — Step 7 spec
> - DevBrain decision `fc1a62bb` — compliance per-project profiles refactor (Option A locked)
> - DevBrain decision `bbeb549d` — Step 6 complete (substrate ready)
>
> **Verification gate:** P6 + P7 postulates ship + 5 per-rule postulates ship. All 14 prior postulates still pass. Re-enable `test_brief_filters_by_compliance_profiles` (was skipped in 5d).

---

## 1. Locked decisions (carried forward)

From `fc1a62bb` (compliance refactor) and the playbook §5:

| # | Decision |
|---|---|
| 1 | **Storage shape — Option A**: `compliance_profiles text[]` column on `devbrain.memory` with GIN index. `compliance_profiles_enabled text[]` column on `devbrain.projects`. |
| 2 | **Profile semantics — explicit opt-in**: A rule with `compliance_profiles = NULL` or `[]` applies to NO project. Defaults must be safe — projects only get rules they explicitly enabled. P6 enforces. |
| 3 | **Curator filter** at brief-generation time uses array intersection: `WHERE compliance_profiles && project.compliance_profiles_enabled`. Drop the graceful-fallback in `brief.py:_load_rules` since columns now exist. |
| 4 | **Lint contract**: any rule with non-empty `compliance_profiles` MUST have a postulate test. CI fails if missing. New CLI: `devbrain rules lint`. |
| 5 | **eval_hipaa is dropped from the roadmap** — HIPAA dissolves into profile-tagged rule rows. Same for any future `eval_*regulation*` agent. |

## 2. The 5 seeded rules

Each ships in Phase 7c as a `tier='rule'` memory row + a per-rule postulate test:

| # | Profile | Rule | Postulate target |
|---|---|---|---|
| 1 | `hipaa` | "PHI columns must not appear in unstructured logs" | `phi_*` table reads logged with column names → fail |
| 2 | `hipaa` | "Audit log writes required for every PHI read/write at service-method boundary" | Service method touches `phi_*` without `audit_log` write → fail |
| 3 | `soc2` | "Secrets must not appear in error messages or stack traces" | Exception path emits `password=`/`token=`/`secret=` literal substrings → fail |
| 4 | `ferpa` | "Student identifiers (SSID, full name) must not leave the EMR boundary in unredacted form" | Function returning to non-EMR caller emits `student.ssid`/`student.full_name` unredacted → fail |
| 5 | `soc2` | "Bulk exports require explicit DPO sign-off claim in the request" | Endpoint with `bulk_export` semantics without sign-off check → fail |

Approved by Patrick, 2026-05-05. Conservative regulatory-grade choices that match the pattern-detection enforcement model (not runtime PHI gating — that stays in BrightBrain's RBAC layer).

## 3. Sub-PR sequence

```
7a (migration + P6/P7) → 7b (filter + lint CLI) → 7c (5 seeded rules + per-rule postulates — Phase 3 closeout)
```

Each PR independently reviewable. Merge on CI green before next.

---

## 4. Phase 7a — migration + filter columns + P6/P7

**Files:**
- Create: `migrations/022_compliance_profiles.sql`
- Test: `factory/tests/test_migration_022.py`
- Modify: `factory/tests/test_curator_brief.py` — un-skip `test_brief_filters_by_compliance_profiles`
- Postulate: `tests/postulates/test_p6_empty_profiles_excludes.py`
- Postulate: `tests/postulates/test_p7_profile_intersection.py`
- Modify: `factory/tests/conftest.py` — extend `project_factory` to accept `compliance_profiles_enabled` kwarg if not already; same for `memory_factory` accepting `compliance_profiles`

### Task 7a-1: Migration 022

```sql
-- Atlas Step 7 — Per-project compliance profiles
-- ============================================================================
--
-- Adds the substrate for filtering rules by compliance profile membership.
-- Per the playbook §5 + DevBrain decision fc1a62bb (Option A locked):
--
--   1. devbrain.memory.compliance_profiles text[] — rules tag themselves
--      with one or more profiles ('hipaa', 'soc2', 'ferpa', etc.). NULL/[]
--      means the rule applies to NO project (explicit opt-in semantics).
--   2. devbrain.projects.compliance_profiles_enabled text[] — projects
--      enable specific profiles. Curator brief filters rules by intersection.
--   3. GIN index on memory.compliance_profiles for the curator hot-path
--      query: WHERE compliance_profiles && %s::text[]

ALTER TABLE devbrain.memory
    ADD COLUMN IF NOT EXISTS compliance_profiles TEXT[];

ALTER TABLE devbrain.projects
    ADD COLUMN IF NOT EXISTS compliance_profiles_enabled TEXT[];

CREATE INDEX IF NOT EXISTS idx_memory_compliance_profiles_gin
    ON devbrain.memory USING GIN (compliance_profiles)
    WHERE compliance_profiles IS NOT NULL;
```

Apply locally. Backfill `schema_migrations`. Schema-assertion test in `test_migration_022.py` (4 tests: 2 columns + GIN index + partial predicate). Commit:
```
feat(memory): migration 022 — compliance_profiles columns + GIN index (Atlas Step 7a)
```

### Task 7a-2: Re-enable the deferred brief test

`factory/tests/test_curator_brief.py` — locate `test_brief_filters_by_compliance_profiles` (currently `@pytest.mark.skip(...)` with a "Step 7" reason). Remove the skip marker. Verify the test now passes (the migration columns exist + the brief.py filter logic from Phase 5d already handles them via the try/except — once the columns exist, the if-profiles branch fires).

Commit:
```
test(curator): re-enable compliance_profiles filter test now that columns exist (Atlas Step 7a)
```

### Task 7a-3: P6 — empty profiles excludes

`tests/postulates/test_p6_empty_profiles_excludes.py`:

```python
"""P6 — Rules with empty compliance_profiles are NOT included in any project's brief.

POSTULATE
---------
A tier='rule' row with compliance_profiles = NULL or [] does NOT appear in
any project's curator brief.rules — explicit opt-in semantics. Projects
only get rules they tagged for.
"""
```

Test: seed a rule with `compliance_profiles=NULL` and another with `compliance_profiles=[]`. Generate a brief for a project with `compliance_profiles_enabled=['hipaa']`. Assert neither rule appears in `brief.rules`.

### Task 7a-4: P7 — profile intersection

`tests/postulates/test_p7_profile_intersection.py`:

```python
"""P7 — A project with compliance_profiles_enabled=['hipaa'] sees only HIPAA-tagged rules.

POSTULATE
---------
The curator brief's `rules` section for a project is the set of memories
where tier='rule' AND archived_at IS NULL AND
compliance_profiles && project.compliance_profiles_enabled.
"""
```

Test: seed 3 rules (one each tagged `['hipaa']`, `['soc2']`, `['hipaa', 'soc2']`). Project enables `['hipaa']`. Generate brief. Assert: HIPAA-only rule and the both-tagged rule appear; SOC2-only rule does NOT.

### Task 7a-5: Open PR

PR title: `feat(memory): Atlas Step 7a — compliance_profiles migration + P6/P7 postulates`

---

## 5. Phase 7b — curator filter activation + rules lint CLI

**Files:**
- Modify: `factory/curator/brief.py` — drop the try/except graceful-fallback in `_load_rules` and `_load_enabled_profiles` (columns now guaranteed to exist after 7a)
- Modify: `factory/cli.py` — add `devbrain rules lint` subcommand
- Create: `factory/curator/rules_lint.py` — lint logic
- Test: `factory/tests/test_curator_rules_lint.py`
- Modify: `.github/workflows/test.yml` — add `devbrain rules lint` step to CI

### Task 7b-1: Drop graceful-fallback in `brief.py`

Read `factory/curator/brief.py:_load_enabled_profiles` and `_load_rules`. They currently have try/except wrapping the SQL that references `compliance_profiles_enabled` / `compliance_profiles`. With 7a's migration applied, the try/except is no longer needed — drop it. Simplify to a clean SQL path.

Verify by running existing brief tests + the just-re-enabled `test_brief_filters_by_compliance_profiles`.

Commit:
```
refactor(curator): drop graceful-fallback in brief filter — columns now guaranteed (Atlas Step 7b)
```

### Task 7b-2: `devbrain rules lint` CLI

Create `factory/curator/rules_lint.py`:

```python
"""Lint compliance-profile-tagged rules: every rule with non-empty
compliance_profiles MUST have a postulate test.

Public API:
- find_unverified_rules(conn) -> list[UnverifiedRule]
- run_lint(conn) -> int  # exit code, 0 if clean, 1 if violations

Discovery:
- Walk tests/postulates/ for files matching test_p*_*.py
- For each profile-tagged rule in devbrain.memory, check that at least one
  postulate test file mentions either the rule's id or a slugified version
  of its title in its module docstring or content.
- Heuristic match — not perfect, but catches the common case where someone
  adds a rule but forgets the postulate.
"""
```

The matching heuristic for v3.0: scan `tests/postulates/*.py` for any reference to the rule's UUID OR its slugified title. If neither found, the rule is "unverified" → lint failure.

Add Click subcommand in `factory/cli.py`:

```python
@cli.group()
def rules():
    """Compliance rule operations."""

@rules.command("lint")
def cmd_rules_lint():
    """Verify every profile-tagged rule has a postulate test."""
    from curator.rules_lint import run_lint
    sys.exit(run_lint(get_db_conn()))
```

Tests in `factory/tests/test_curator_rules_lint.py`:
- `find_unverified_rules` returns empty when all rules have matching postulate
- Returns the rule when no postulate references it
- Cross-project safety: scans all projects (lint is project-agnostic)
- Heuristic match works on UUID literal AND slugified title
- Empty `compliance_profiles` rules are not flagged (only profile-tagged ones need verification)

Coverage gate ≥ 85%.

Commit:
```
feat(curator): rules lint CLI — verify every profile-tagged rule has a postulate (Atlas Step 7b)
```

### Task 7b-3: Wire into CI

Modify `.github/workflows/test.yml` — add a step to the `pytest-db` job (after migrations, before pytest):

```yaml
      - name: Lint compliance rules (devbrain rules lint)
        env:
          DEVBRAIN_DB_HOST: localhost
          DEVBRAIN_DB_HOST_PORT: 5432
          DEVBRAIN_DB_USER: devbrain
          DEVBRAIN_DB_PASSWORD: devbrain-ci
          DEVBRAIN_DB_NAME: devbrain
        run: |
          cd factory && python -m curator.rules_lint
```

(Calling the module directly avoids pulling in the full `devbrain` CLI parser graph for a lint check.)

Commit:
```
ci(tests): run rules lint as part of pytest-db job (Atlas Step 7b)
```

### Task 7b-4: Open PR

PR title: `feat(curator): Atlas Step 7b — filter activation + rules lint CLI`

---

## 6. Phase 7c — 5 seeded rules + per-rule postulates

**Files:**
- Create: `migrations/023_seeded_compliance_rules.sql` — INSERT 5 rules into `devbrain.memory`
- Postulate: `tests/postulates/test_rule_phi_no_unstructured_logs.py`
- Postulate: `tests/postulates/test_rule_audit_log_required.py`
- Postulate: `tests/postulates/test_rule_secrets_in_errors.py`
- Postulate: `tests/postulates/test_rule_ferpa_student_id_redacted.py`
- Postulate: `tests/postulates/test_rule_bulk_export_signoff.py`

### Task 7c-1: Migration 023 — seed the 5 rules

```sql
-- Atlas Step 7 — Seeded compliance rules (HIPAA + SOC2 + FERPA)
-- ============================================================================
--
-- Five regulatory-grade rules across 3 compliance profiles. Each ships with
-- a postulate test that proves its enforcement contract.
--
-- Profile distribution: 2 hipaa + 1 ferpa + 2 soc2.
--
-- All rules use the canonical 'devbrain' project so they're available
-- substrate. Project enablement (compliance_profiles_enabled) determines
-- which projects get them in their briefs.

INSERT INTO devbrain.memory
    (project_id, kind, title, content, tier, strength, compliance_profiles)
SELECT
    (SELECT id FROM devbrain.projects WHERE name = 'devbrain' LIMIT 1),
    kind,
    title,
    content,
    'rule',
    1.0,
    profiles
FROM (VALUES
    ('decision'::text,
     'PHI columns must not appear in unstructured logs',
     'Reads/writes against phi_* tables must redact column values before any logger call. Loggers (info/debug/warn/error) emitting raw PHI table contents are a HIPAA 164.312(a)(2)(iv) violation.',
     ARRAY['hipaa']::text[]),
    ('decision',
     'Audit log writes required for every PHI read/write at service-method boundary',
     'HIPAA 164.312(b) audit controls — every service method that reads or writes phi_* tables must emit an audit_log row with actor + action + resource. Method-level enforcement (not row-level): one audit_log entry per service-method invocation.',
     ARRAY['hipaa']::text[]),
    ('decision',
     'Secrets must not appear in error messages or stack traces',
     'SOC2 CC6.1 — exception paths must redact password=, token=, api_key=, secret=, bearer + similar literal substrings before propagating to logs or HTTP responses. Stack-trace serialization must scrub frame locals of the same patterns.',
     ARRAY['soc2']::text[]),
    ('decision',
     'Student identifiers (SSID, full name) must not leave the EMR boundary in unredacted form',
     'FERPA 99.31 — any function returning a student.ssid or student.full_name to a non-EMR caller (Google Workspace integration, public API, third-party webhook) must redact or hash the identifier. Within the EMR boundary, raw identifiers are permitted.',
     ARRAY['ferpa']::text[]),
    ('decision',
     'Bulk exports require explicit DPO sign-off claim in the request',
     'SOC2 CC6.7 — endpoints with bulk export semantics (returning >100 rows of regulated data) must validate a request claim signed by the DPO role. Single-record reads are exempt; the threshold is per-request payload size.',
     ARRAY['soc2']::text[])
) AS r(kind, title, content, profiles)
ON CONFLICT DO NOTHING;
```

Apply locally. Backfill `schema_migrations`. The migration is idempotent (re-runnable safely via ON CONFLICT). Verify by querying `SELECT title, compliance_profiles FROM devbrain.memory WHERE tier='rule' AND compliance_profiles IS NOT NULL`.

Commit:
```
feat(memory): migration 023 — seed 5 compliance rules across HIPAA/SOC2/FERPA (Atlas Step 7c)
```

### Task 7c-2: 5 per-rule postulates

Each postulate test follows the contract: arrange a code-pattern that violates the rule, run the rule's matcher, assert violation detected. Five files, each in `tests/postulates/`:

1. `test_rule_phi_no_unstructured_logs.py` — assertion: a Python source containing `logger.info(f"phi_audit_log row: {row}")` is detected as violating; a redacted version `logger.info(f"phi_audit_log accessed (id={row.id})")` is NOT detected.

2. `test_rule_audit_log_required.py` — assertion: a service method that calls `phi_audit_log_repo.read(id)` without a corresponding `audit_log_writer.record(...)` call is detected; a method that does both is NOT detected.

3. `test_rule_secrets_in_errors.py` — assertion: an exception body containing `f"Auth failed: password={p}"` is detected; one with `"Auth failed: password=<redacted>"` is NOT detected.

4. `test_rule_ferpa_student_id_redacted.py` — assertion: a Google Workspace integration function returning `student.ssid` raw is detected; one returning `hash_ssid(student.ssid)` is NOT detected.

5. `test_rule_bulk_export_signoff.py` — assertion: a `bulk_export` endpoint without a `request.dpo_signoff` check is detected; one with the check is NOT detected.

**Implementation note for v3.0:** the "matcher" for each rule is a simple regex/AST scan against synthetic Python code strings constructed in the test. The tests prove the rule's enforcement *contract* (what counts as violating). Production-grade matching (running actual code through an AST analyzer) is Phase 3.x scope per the design's "agent-based default + declarative JSON for regulatory" mode.

For v3.0, each postulate's matcher is implemented inline in the test file — small, self-contained. When eval_security and eval_test agents grow in Phase 3.x, they'll consume these contracts as guidance.

Commit each postulate file individually OR bundle:
```
test(postulates): 5 seeded compliance rule contracts (Atlas Step 7c)
```

### Task 7c-3: Open PR — Phase 3 closeout

PR title: `feat(curator): Atlas Step 7c — 5 seeded compliance rules (Phase 3 done)`

Body should highlight:
- Phase 3 of broader DevBrain roadmap is complete after this merge
- All 7 Atlas steps shipped (P1-P7 + 5 per-rule postulates = 14+5 = 19 postulates green in CI)
- Step 8+ (more eval agents) and broader Phase 5 (graph/AGE) and Phase 6 (cognify/memify) become the next direction

---

## 7. Verification matrix — when is Step 7 done?

| Gate | Source |
|---|---|
| P6 (empty profiles excludes) passes | 7a |
| P7 (profile intersection) passes | 7a |
| `test_brief_filters_by_compliance_profiles` re-enabled and passes | 7a |
| 5 per-rule postulates pass | 7c |
| `devbrain rules lint` passes (every profile-tagged rule has a postulate) | 7b + 7c |
| All 14 prior postulates still pass | regression |
| `factory/curator/rules_lint.py` ≥ 85% coverage | 7b |
| Existing tests still pass | every PR |

After all three sub-PRs merge, **Atlas Step 7 is done. Phase 3 of the broader DevBrain roadmap is complete.**

---

## 8. Out of scope (explicitly carried forward)

- **AST-based pattern matching for rules** — v3.0 uses inline regex/string match in postulate tests. Phase 3.x can add `eval_security` / `eval_test` agents that perform real AST analysis using the same rule contracts.
- **Per-project rule overrides** — projects enable profiles wholesale; per-rule overrides (e.g., "enable HIPAA except rule X") is YAGNI for v3.0.
- **Rule precision tracking dashboard** — `memory_ledger` audits every transition; richer dashboards are Phase 3.x.
- **Profile name validation / namespace** — free-form strings for v3.0 (`hipaa`, `soc2`, `ferpa`). A reserved-names list is Phase 3.x advisory.
