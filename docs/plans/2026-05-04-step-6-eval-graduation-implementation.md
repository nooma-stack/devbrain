# Atlas Step 6 — Eval Agents + Lesson Graduation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** Ship Step 6 of Atlas / Phase 3: two eval agents (eval_security + eval_test), a three-signal graduation pipeline that promotes lessons to rules and demotes low-precision rules back, and a curator self-introspection refinement path. Adds 2 new postulates (P4 + P5).

**Architecture:** Sequential eval calls share a warm prompt cache (cost optimization over latency). Three feedback signals fire at end of REVIEWING phase. Graduation = N=3 consecutive successful preventions in a 90-day window. Demotion = precision < 50% over 30-day window. Refinement = curator self-introspection with end_session enrichment as supplemental.

**Tech stack:** Python 3.14, pytest, psycopg2, pgvector, Pydantic v2, TypeScript MCP server (Node 20), Postgres 16. (Same stack as Step 5.)

**Design reference:** `docs/plans/2026-05-04-step-6-eval-graduation-design.md` (locked).

**Conventions to know before starting:**
- Tests live in `factory/tests/` and run from `cd factory && pytest tests/...`
- Postulates in `tests/postulates/test_pN_<slug>.py` — strict-xfail until feature lands, then flip
- Sequential numbered migrations in `migrations/0NN_<slug>.sql`. Step 6 uses **019**
- Pydantic v2 syntax (model_validate, model_dump, ConfigDict)
- Auth switch dance for push: `gh auth switch -u nooma-stack` for push + PR create, switch back to PatrickLHT
- Each phase ships as own PR. Merge on CI green before next phase

---

## Phase 6a — Migration 019 + Pydantic types + graduation module skeleton

**Files:**
- Create: `migrations/019_lesson_graduation.sql`
- Create: `factory/curator/eval/__init__.py` (empty)
- Create: `factory/curator/eval/types.py`
- Create: `factory/curator/graduation.py` (skeleton — constants + signature stubs)
- Create: `factory/curator/refinement.py` (skeleton)
- Test: `factory/tests/test_migration_019.py`
- Test: `factory/tests/test_curator_eval_types.py`

### Task 6a-1: Migration 019

Create `migrations/019_lesson_graduation.sql`:

```sql
-- Atlas Step 6 — Lesson graduation tracking
-- ============================================================================
--
-- Three columns + one index for the graduation pipeline:
--   1. current_streak — consecutive successful preventions (signal #3
--      increments, signal #1 resets)
--   2. graduated_at — timestamp when tier transitioned 'lesson' -> 'rule'
--   3. demoted_at — timestamp when tier transitioned 'rule' -> 'lesson'
--
-- Index optimizes the graduation candidate query at end of every
-- REVIEWING phase.

ALTER TABLE devbrain.memory
    ADD COLUMN IF NOT EXISTS current_streak INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS graduated_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS demoted_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_memory_graduation_candidates
    ON devbrain.memory (last_hit DESC)
    WHERE tier = 'lesson' AND current_streak >= 3 AND archived_at IS NULL;
```

Apply locally:
```bash
docker compose exec -T devbrain-db psql -U devbrain -d devbrain < migrations/019_lesson_graduation.sql
```

Verify schema. Commit:
```
feat(memory): migration 019 — current_streak + graduated_at + demoted_at on devbrain.memory (Atlas Step 6a)
```

### Task 6a-2: Migration 019 schema-assertion test

Create `factory/tests/test_migration_019.py` with 4 tests parallel to `test_migration_017.py`:
- columns exist
- index exists with correct partial predicate
- defaults work (`current_streak DEFAULT 0`)
- NULL allowed for graduated_at + demoted_at

Commit:
```
test(memory): migration 019 schema assertions (Atlas Step 6a)
```

### Task 6a-3: Pydantic types — `factory/curator/eval/types.py`

```python
"""Versioned Pydantic models for eval findings.

Stable contract consumed by the fix-loop implementer + graduation pipeline.
Bumping a model goes via Pydantic discriminated union on `version`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class EvalFinding(BaseModel):
    """A single finding from an eval agent."""

    model_config = ConfigDict(frozen=True)

    rule_id: UUID | None  # NULL if finding is from a heuristic, not a memory row
    severity: Literal["critical", "important", "minor"]
    file: str
    line: int | None
    message: str
    fix_hint: str
    relevant_memory_id: UUID | None  # which memory in brief surfaced this; NULL if missed


class EvalResult(BaseModel):
    """Full result of one eval agent run."""

    model_config = ConfigDict(frozen=True)

    version: Literal["1.0"]
    job_id: UUID
    agent_name: Literal["eval_security", "eval_test"]
    findings: list[EvalFinding]
    elapsed_ms: int
    started_at: datetime
    error: str | None = None  # set if agent failed; findings will be []
```

Test in `factory/tests/test_curator_eval_types.py` — 6 round-trip + validation tests:
- `EvalFinding` round-trip
- `EvalResult` round-trip
- Rejects unknown `severity`
- Rejects unknown `agent_name`
- Rejects unknown `version`
- Allows `rule_id=None` (heuristic findings)

100% coverage required. Commit:
```
feat(curator): eval finding + result Pydantic types (Atlas Step 6a)
```

### Task 6a-4: Graduation + refinement skeleton

Create `factory/curator/graduation.py`:

```python
"""Three-signal feedback loop for lesson graduation + rule demotion.

Public API:
- apply_feedback_signals(conn, job_id, brief, eval_results)
- demote_low_precision_rules(conn, project_id)
"""
from __future__ import annotations

# Tunable constants — change here if real-world data shows them wrong.
GRADUATION_STREAK_THRESHOLD = 3
GRADUATION_FRESHNESS_WINDOW = "90 days"
DEMOTION_PRECISION_THRESHOLD = 0.50
DEMOTION_WINDOW = "30 days"


def apply_feedback_signals(conn, job_id, brief, eval_results):
    """Stub. Implementation lands in 6c."""
    raise NotImplementedError("ships in Phase 6c")


def demote_low_precision_rules(conn, project_id):
    """Stub. Implementation lands in 6c."""
    raise NotImplementedError("ships in Phase 6c")
```

Create `factory/curator/refinement.py`:

```python
"""Curator self-introspection — proposes applies_when widening for memories
that should have been in the brief but weren't (signal #2).

Public API:
- queue_refinement(conn, finding)
- refine_applies_when(conn, project_id)

Implementation lands in 6d.
"""
from __future__ import annotations


def queue_refinement(conn, finding):
    """Stub. Implementation lands in 6d."""
    raise NotImplementedError("ships in Phase 6d")


def refine_applies_when(conn, project_id):
    """Stub. Implementation lands in 6d."""
    raise NotImplementedError("ships in Phase 6d")
```

Commit:
```
feat(curator): graduation + refinement module skeletons (Atlas Step 6a)
```

### Task 6a-5: Open PR for Phase 6a

Push branch `feat/atlas-step-6a-foundation`, open PR titled:
> `feat(memory): Atlas Step 6a — migration 019 + eval types + graduation skeleton`

Body should reference design doc, list the 4 tests + 6 type tests, note that downstream phases (6b-6e) implement the stubs.

Merge on CI green.

---

## Phase 6b — Eval runner + eval_security + eval_test

**Files:**
- Create: `factory/curator/eval/runner.py`
- Create: `factory/curator/eval/eval_security.py`
- Create: `factory/curator/eval/eval_test.py`
- Create: `factory/curator/eval/prompts/eval_security.md`
- Create: `factory/curator/eval/prompts/eval_test.md`
- Test: `factory/tests/test_curator_eval_runner.py`
- Test: `factory/tests/test_eval_security.py`
- Test: `factory/tests/test_eval_test.py`

### Task 6b-1: Eval runner with warm-cache invocation

Create `factory/curator/eval/runner.py`:

```python
"""Sequential eval invocation with warm prompt cache.

run_evals() spawns one claude process. eval_security runs first and
instantiates the cache (project + spec + brief + plan + diff). eval_test
runs second against the same cache, hitting cached input at ~10% cost.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from curator.eval.types import EvalFinding, EvalResult

logger = logging.getLogger(__name__)

# Path to prompts dir — resolved relative to this module.
_PROMPTS_DIR = Path(__file__).parent / "prompts"


def run_evals(
    conn: Any,
    job_id: UUID,
    brief: dict,
    plan: str,
    diff: str,
) -> list[EvalResult]:
    """Run eval_security then eval_test, sharing a warm prompt cache.

    Persists findings to factory_artifacts. Returns the EvalResult list.
    """
    cached_context = _build_cached_context(brief, plan, diff)
    results: list[EvalResult] = []

    for agent_name, prompt_file in [
        ("eval_security", "eval_security.md"),
        ("eval_test", "eval_test.md"),
    ]:
        try:
            result = _invoke_agent(
                job_id=job_id,
                agent_name=agent_name,
                prompt_path=_PROMPTS_DIR / prompt_file,
                cached_context=cached_context,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("run_evals: %s failed", agent_name)
            result = EvalResult(
                version="1.0",
                job_id=job_id,
                agent_name=agent_name,
                findings=[],
                elapsed_ms=0,
                started_at=datetime.now(timezone.utc),
                error=str(exc)[:500],
            )
        results.append(result)

    _persist_findings(conn, job_id, results)
    return results


def _build_cached_context(brief: dict, plan: str, diff: str) -> str:
    """Format the shared eval context for prompt caching.

    All three eval agents (current and future) read the same context, so
    this is the cache-hit hot path.
    """
    return (
        f"## Brief\n\n{json.dumps(brief, indent=2)}\n\n"
        f"## Plan\n\n{plan}\n\n"
        f"## Diff\n\n```diff\n{diff}\n```\n"
    )


def _invoke_agent(
    job_id: UUID,
    agent_name: str,
    prompt_path: Path,
    cached_context: str,
) -> EvalResult:
    """Invoke claude with the eval prompt + cached context.

    First call instantiates cache; second call reuses it (claude's prompt
    cache TTL is 5 min, well within typical job phase duration).
    """
    started_at = datetime.now(timezone.utc)
    start = time.perf_counter()

    system_prompt = prompt_path.read_text()
    user_prompt = cached_context

    # Spawn claude — stdin payload, JSON output expected.
    proc = subprocess.run(
        ["claude", "-p", "--output-format=json", "--system-prompt", system_prompt],
        input=user_prompt,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude exit {proc.returncode}: {proc.stderr[:500]}")

    response = json.loads(proc.stdout)
    findings = _parse_findings(agent_name, response)

    elapsed_ms = int((time.perf_counter() - start) * 1000)
    return EvalResult(
        version="1.0",
        job_id=job_id,
        agent_name=agent_name,
        findings=findings,
        elapsed_ms=elapsed_ms,
        started_at=started_at,
    )


def _parse_findings(agent_name: str, response: dict) -> list[EvalFinding]:
    """Delegate finding parsing to the per-agent module."""
    if agent_name == "eval_security":
        from curator.eval.eval_security import parse
    elif agent_name == "eval_test":
        from curator.eval.eval_test import parse
    else:
        raise ValueError(f"unknown agent_name: {agent_name}")
    return parse(response)


def _persist_findings(conn: Any, job_id: UUID, results: list[EvalResult]) -> None:
    """Write findings to factory_artifacts.

    One row per finding, JSON-serialized with the existing factory_artifacts
    shape: {rule_id, severity, file, line, message, fix_hint}.
    """
    with conn.cursor() as cur:
        for result in results:
            for finding in result.findings:
                cur.execute(
                    "INSERT INTO devbrain.factory_artifacts "
                    "(job_id, agent_name, finding) VALUES (%s, %s, %s::jsonb)",
                    (job_id, result.agent_name, finding.model_dump_json()),
                )
    conn.commit()
```

Tests in `factory/tests/test_curator_eval_runner.py` — mock subprocess.run, verify:
- Both agents invoked sequentially in correct order
- Cached context built correctly
- Findings persisted to factory_artifacts
- Agent failure produces empty findings + error annotation, but other agent still runs
- Round-trip EvalResult through serialization

Coverage gate ≥ 85%.

### Task 6b-2: eval_security + eval_test parsers

Create `factory/curator/eval/eval_security.py`:

```python
"""eval_security finding parser.

eval_security checks: auth, injection, secret leakage, dependency CVE.
The prompt instructs claude to emit a JSON list of findings; this parser
maps that to EvalFinding objects.
"""
from __future__ import annotations

from uuid import UUID

from curator.eval.types import EvalFinding


def parse(response: dict) -> list[EvalFinding]:
    """Map claude's JSON response to EvalFinding list.

    Expected response shape from the prompt:
    {
      "findings": [
        {
          "rule_id": "uuid-or-null",
          "severity": "critical|important|minor",
          "file": "path/to/file.py",
          "line": 42,
          "message": "...",
          "fix_hint": "...",
          "relevant_memory_id": "uuid-or-null"
        }
      ]
    }
    """
    raw = response.get("findings", [])
    return [_parse_one(item) for item in raw]


def _parse_one(item: dict) -> EvalFinding:
    return EvalFinding(
        rule_id=UUID(item["rule_id"]) if item.get("rule_id") else None,
        severity=item["severity"],
        file=item["file"],
        line=item.get("line"),
        message=item["message"],
        fix_hint=item.get("fix_hint", ""),
        relevant_memory_id=(
            UUID(item["relevant_memory_id"])
            if item.get("relevant_memory_id") else None
        ),
    )
```

`factory/curator/eval/eval_test.py` — same structure, different prompt focus (coverage of diff, test quality, brittleness).

Tests in `factory/tests/test_eval_security.py` and `test_eval_test.py` — exercise the parser with synthetic responses including edge cases (missing fields, NULL rule_id, NULL relevant_memory_id, invalid severity).

### Task 6b-3: Eval prompts

Create `factory/curator/eval/prompts/eval_security.md`:

```markdown
You are eval_security, a domain-specialized eval agent for the DevBrain
factory. You inspect a code diff for security violations.

## Your task

Read the brief, plan, and diff (provided in user message). Identify:
- Authentication / authorization gaps
- Injection vulnerabilities (SQL, command, prompt)
- Secret leakage (logs, error messages, fixture data)
- Dependency CVE references (only if the diff touches dependencies)

For each finding, surface which memory in the brief covers it (if any) by
setting `relevant_memory_id` to that memory's id. If no in-brief memory
covers the finding, set `relevant_memory_id: null`.

## Output format

Strict JSON. NO PROSE OUTSIDE THE JSON.

{
  "findings": [
    {
      "rule_id": "uuid-or-null",
      "severity": "critical|important|minor",
      "file": "factory/whatever.py",
      "line": 42,
      "message": "Brief finding description.",
      "fix_hint": "How to address.",
      "relevant_memory_id": "uuid-or-null"
    }
  ]
}

If you find no violations, return: {"findings": []}

## Severity guidance

- critical: exploitable now in this diff (e.g., SQL injection vector)
- important: would become exploitable in production (e.g., logged secret)
- minor: defense-in-depth gap (e.g., missing rate limit on a public endpoint)
```

`factory/curator/eval/prompts/eval_test.md` — parallel structure, focuses on:
- Coverage of the diff (new code without tests)
- Test quality (e.g., assertions test mocks not behavior)
- Brittleness (snapshot tests that lock irrelevant detail)
- Missing edge cases relative to the spec

### Task 6b-4: Open PR for Phase 6b

```
feat(curator): Atlas Step 6b — eval runner + eval_security + eval_test
```

Body covers: warm-cache pattern, factory_artifacts integration, findings shape, prompt files for both agents, mock-LLM unit tests.

Merge on CI green.

---

## Phase 6c — Graduation pipeline (3 signal handlers + demote sweep)

**Files:**
- Modify: `factory/curator/graduation.py` (replace stubs)
- Test: `factory/tests/test_curator_graduation.py`
- Postulate: `tests/postulates/test_p4_lesson_graduation.py`

### Task 6c-1: Signal handlers

Replace stubs in `factory/curator/graduation.py` with:

```python
def apply_feedback_signals(conn, job_id, brief, eval_results):
    """For each memory in brief, fire signal 1 (failure) or signal 3 (success).
    For each finding NOT in brief, queue signal 2 (refinement)."""
    in_brief_ids = _collect_brief_memory_ids(brief)
    findings_by_memory = _index_findings_by_memory(eval_results)

    for mid in in_brief_ids:
        if mid in findings_by_memory:
            _signal_failure(conn, mid)
        else:
            _signal_success(conn, mid)

    # Signal 2: findings whose relevant_memory_id is NOT in brief
    from curator.refinement import queue_refinement
    for result in eval_results:
        for finding in result.findings:
            if (finding.relevant_memory_id
                    and finding.relevant_memory_id not in in_brief_ids):
                queue_refinement(conn, finding)


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
            "RETURNING tier, current_streak",
            (memory_id,),
        )
        row = cur.fetchone()
        if row is None:
            return
        tier, streak = row
        if tier == "lesson" and streak >= GRADUATION_STREAK_THRESHOLD:
            _graduate(conn, memory_id)


def _graduate(conn, memory_id):
    """Promote tier='lesson' to tier='rule'. Ledger row written by AFTER trigger."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory "
            "SET tier = 'rule', graduated_at = NOW() "
            "WHERE id = %s AND tier = 'lesson'",
            (memory_id,),
        )
    conn.commit()


def demote_low_precision_rules(conn, project_id):
    """Sweep rules with precision < 50% over 30-day window. Demote to lesson."""
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


def _collect_brief_memory_ids(brief):
    """Extract all memory IDs referenced in the brief."""
    ids = set()
    for ref in brief.get("rules", []):
        ids.add(ref["id"])
    for ref in brief.get("lessons", []):
        ids.add(ref["id"])
    for ref in brief.get("relevant_decisions", []):
        ids.add(ref["id"])
    return ids


def _index_findings_by_memory(eval_results):
    """Map relevant_memory_id -> list of findings."""
    index = {}
    for result in eval_results:
        for finding in result.findings:
            mid = finding.relevant_memory_id
            if mid:
                index.setdefault(mid, []).append(finding)
    return index
```

### Task 6c-2: Graduation tests

`factory/tests/test_curator_graduation.py` — unit tests covering:
- `_signal_failure` resets streak, increments hit_count
- `_signal_success` increments streak + effective_hit_count + last_hit
- `_signal_success` triggers `_graduate` at streak == 3
- `_signal_success` does NOT graduate at streak == 2
- `_signal_success` does NOT graduate non-lesson tiers
- Graduation sets `graduated_at` and tier='rule'
- `demote_low_precision_rules` demotes rules with precision < 0.5
- `demote_low_precision_rules` ignores rules with precision >= 0.5
- `demote_low_precision_rules` ignores stale rules (last_hit > 30 days ago)
- Demotion sets `demoted_at`, tier='lesson', current_streak=0
- Cross-project safety: demotion scoped by project_id

Coverage gate ≥ 90%.

### Task 6c-3: P4 postulate — lesson graduation

`tests/postulates/test_p4_lesson_graduation.py`:

```python
"""P4 — lesson graduation.

POSTULATE
---------
A tier='lesson' row that:
  1. Is included in 3 consecutive briefs
  2. Has 3 successful preventions (signal #3) in those briefs
  3. Has last_hit within the 90-day freshness window

...transitions to tier='rule', graduated_at is set, and a memory_ledger
row records the transition.
"""
```

Test scenario: seed a `tier='lesson'` row with `current_streak=2`. Fire signal #3 (success) once. Assert: tier flips to 'rule', graduated_at set, ledger row exists.

### Task 6c-4: Open PR for Phase 6c

```
feat(curator): Atlas Step 6c — graduation pipeline + P4 postulate
```

Merge on CI green.

---

## Phase 6d — Refinement path (curator self-introspection)

**Files:**
- Modify: `factory/curator/refinement.py` (replace stubs)
- Migration: `migrations/020_refinement_queue.sql` (new table for queued refinements)
- Test: `factory/tests/test_curator_refinement.py`
- Test: `factory/tests/test_migration_020.py`

### Task 6d-1: Migration 020 — refinement queue

```sql
-- Atlas Step 6 — Refinement queue
-- ============================================================================
--
-- Signal #2 cases (NOT in brief but should have been) get queued here.
-- The curator's refinement pass at end of REVIEWING dequeues entries and
-- proposes applies_when widening for each.

CREATE TABLE IF NOT EXISTS devbrain.refinement_queue (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id    UUID NOT NULL REFERENCES devbrain.memory(id) ON DELETE CASCADE,
    file_pattern TEXT,
    keywords     TEXT[],
    queued_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_at   TIMESTAMPTZ,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_refinement_queue_pending
    ON devbrain.refinement_queue (queued_at)
    WHERE applied_at IS NULL;
```

### Task 6d-2: Refinement implementation

Replace stubs in `factory/curator/refinement.py`:

```python
"""Curator self-introspection — proposes applies_when widening for memories
that should have been in the brief but weren't (signal #2).

v3.0: simple keyword extraction from finding.file + finding.message.
Phase 3.x: smarter heuristic or LLM-driven proposal.
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)


def queue_refinement(conn, finding):
    """Queue a signal-#2 case for end-of-tick refinement.

    finding: an EvalFinding with non-NULL relevant_memory_id that wasn't
    in the brief. Extracts a file_pattern + keywords for matching during
    refinement application.
    """
    if finding.relevant_memory_id is None:
        return

    file_pattern = _file_glob_from_path(finding.file)
    keywords = _extract_keywords(finding.message)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.refinement_queue "
            "(memory_id, file_pattern, keywords) VALUES (%s, %s, %s)",
            (finding.relevant_memory_id, file_pattern, keywords),
        )
    conn.commit()


def refine_applies_when(conn, project_id):
    """Process queued refinements: widen each memory's applies_when."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT q.id, q.memory_id, q.file_pattern, q.keywords
            FROM devbrain.refinement_queue q
            JOIN devbrain.memory m ON m.id = q.memory_id
            WHERE q.applied_at IS NULL
              AND q.queued_at > NOW() - INTERVAL '7 days'
              AND m.project_id = %s
            """,
            (project_id,),
        )
        for queue_id, memory_id, file_pattern, keywords in cur.fetchall():
            try:
                _widen_applies_when(conn, memory_id, file_pattern, keywords)
                cur.execute(
                    "UPDATE devbrain.refinement_queue "
                    "SET applied_at = NOW() WHERE id = %s",
                    (queue_id,),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("refine_applies_when: %s failed", queue_id)
                cur.execute(
                    "UPDATE devbrain.refinement_queue "
                    "SET applied_at = NOW(), error = %s WHERE id = %s",
                    (str(exc)[:500], queue_id),
                )
    conn.commit()


def _widen_applies_when(conn, memory_id, file_pattern, keywords):
    """Add file_pattern + keywords to memory's applies_when, deduped."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT applies_when FROM devbrain.memory WHERE id = %s",
            (memory_id,),
        )
        row = cur.fetchone()
        if row is None:
            return
        current = row[0] or {}
        files = set(current.get("files", []))
        if file_pattern:
            files.add(file_pattern)
        kw_set = set(current.get("keywords", []))
        kw_set.update(keywords or [])

        new_aw = {**current, "files": sorted(files), "keywords": sorted(kw_set)}
        cur.execute(
            "UPDATE devbrain.memory SET applies_when = %s::jsonb WHERE id = %s",
            (json.dumps(new_aw), memory_id),
        )


def _file_glob_from_path(path: str) -> str:
    """Convert specific file path to a directory glob.

    e.g., 'factory/curator/worker.py' -> 'factory/curator/*.py'
    """
    if not path or "/" not in path:
        return path or ""
    parts = path.rsplit("/", 1)
    if len(parts) != 2:
        return path
    dir_part, _filename = parts
    return f"{dir_part}/*.py"


def _extract_keywords(text: str) -> list[str]:
    """Naive keyword extraction: words >= 4 chars, deduped, capped at 5."""
    if not text:
        return []
    words = re.findall(r"\b[a-zA-Z_]{4,}\b", text.lower())
    seen = set()
    keywords = []
    for w in words:
        if w not in seen:
            seen.add(w)
            keywords.append(w)
        if len(keywords) >= 5:
            break
    return keywords
```

### Task 6d-3: Tests

`factory/tests/test_migration_020.py` — schema assertions parallel to migration_017/019 tests.

`factory/tests/test_curator_refinement.py`:
- `queue_refinement` inserts row with file_pattern + keywords
- `refine_applies_when` widens applies_when (preserves existing keys, adds new files + keywords, dedupes)
- Cross-project safety: `refine_applies_when` ignores other projects' queue rows
- Failure path: errored refinement gets `applied_at + error` and doesn't retry
- 7-day window: stale queue entries skipped

Coverage gate ≥ 85%.

### Task 6d-4: Open PR for Phase 6d

```
feat(curator): Atlas Step 6d — refinement path + migration 020 (refinement_queue)
```

Merge on CI green.

---

## Phase 6e — State machine integration + P5 postulate + DB-CI

**Files:**
- Modify: `factory/state_machine.py` — IMPLEMENTING → REVIEWING hook
- Modify: `.github/workflows/test.yml` — extend `pytest-db` allow-list
- Postulate: `tests/postulates/test_p5_rule_demotion.py`
- Test: `factory/tests/test_step6_e2e.py`

### Task 6e-1: State machine hook

`factory/state_machine.py` — locate the `transition()` method and add the IMPLEMENTING→REVIEWING hook (parallel to Step 5d's QUEUED→PLANNING):

```python
if (JobStatus(job.status) == JobStatus.IMPLEMENTING
        and new_status == JobStatus.REVIEWING):
    self._run_eval_phase(job)


def _run_eval_phase(self, job):
    """Run eval agents + graduation + refinement + demotion sweep."""
    from curator.eval.runner import run_evals
    from curator.graduation import (
        apply_feedback_signals,
        demote_low_precision_rules,
    )
    from curator.refinement import refine_applies_when

    brief = self._load_brief(job.id)
    plan = self._load_plan(job.id)
    diff = self._load_diff(job.id)

    eval_results = run_evals(self.conn, job.id, brief, plan, diff)
    apply_feedback_signals(self.conn, job.id, brief, eval_results)
    refine_applies_when(self.conn, job.project_id)
    demote_low_precision_rules(self.conn, job.project_id)
```

`_load_brief`, `_load_plan`, `_load_diff` may already exist or need adding. Read state_machine first to verify.

### Task 6e-2: P5 postulate — rule demotion

`tests/postulates/test_p5_rule_demotion.py`:

```python
"""P5 — rule demotion.

POSTULATE
---------
A tier='rule' row whose effective_hit_count / (hit_count + effective_hit_count)
< 0.50 with last_hit within the 30-day window transitions to tier='lesson',
demoted_at is set, current_streak is reset to 0, and a memory_ledger row
records the transition.
"""
```

Test: seed a rule with hit_count=5, effective_hit_count=3 (precision = 0.375), last_hit = NOW. Run `demote_low_precision_rules`. Assert tier='lesson', demoted_at set, streak=0, ledger row exists.

### Task 6e-3: End-to-end integration test

`factory/tests/test_step6_e2e.py` — full flow:
- Seed project + brief with 3 lessons (current_streak=2 each)
- Mock LLM: eval agents return 0 findings (clean diff)
- Trigger IMPLEMENTING → REVIEWING transition
- Assert: all 3 lessons graduated to rules, ledger has 3 transitions

### Task 6e-4: Extend DB-CI allow-list

Modify `.github/workflows/test.yml` `pytest-db` job:

```yaml
- name: Run postulates + curator integration tests
  run: |
    cd factory && python -m pytest \
      tests/postulates/ \
      tests/test_curator_brief.py \
      tests/test_curator_end_session.py \
      tests/test_curator_eval_runner.py        # NEW
      tests/test_curator_eval_types.py         # NEW
      tests/test_curator_graduation.py         # NEW
      tests/test_curator_refinement.py         # NEW
      tests/test_curator_strength.py \
      tests/test_curator_types.py \
      tests/test_curator_worker.py \
      tests/test_eval_security.py              # NEW
      tests/test_eval_test.py                  # NEW
      tests/test_migration_017.py \
      tests/test_migration_018.py \
      tests/test_migration_019.py              # NEW
      tests/test_migration_020.py              # NEW
      tests/test_step6_e2e.py                  # NEW
      tests/test_store_cascade_enqueue.py \
      -v
```

### Task 6e-5: Open PR for Phase 6e

```
feat(curator): Atlas Step 6e — state machine integration + P4/P5 + DB-CI updates (Step 6 done)
```

Body should highlight:
- Step 6 is complete
- P4 + P5 postulates green
- All 10 postulates run on every push (P1-3 + P_cycle/archived/stuck/end_session_isolation/idempotent + P4/P5)

After 6e merges, **Atlas Step 6 is done**. Step 7 (rule engine + per-project compliance profiles + 5 seeded rules) becomes unblocked.

---

## Verification matrix — when is Step 6 done?

| Gate | Source |
|---|---|
| P4 (lesson graduation) passes | 6c |
| P5 (rule demotion) passes | 6e |
| P1, P2, P3, P_cycle, P_archived_mid_cascade, P_stuck_surface_able, P_end_session_isolation, P_end_session_idempotent — all still pass | regression |
| `factory/curator/eval/runner.py` ≥ 85% coverage | 6b |
| `factory/curator/eval/eval_security.py` ≥ 85% | 6b |
| `factory/curator/eval/eval_test.py` ≥ 85% | 6b |
| `factory/curator/eval/types.py` 100% | 6a |
| `factory/curator/graduation.py` ≥ 90% | 6c |
| `factory/curator/refinement.py` ≥ 85% | 6d |
| DB-available CI workflow runs all Step 6 tests on every push | 6e |
| Existing 196+ tests still pass (no regression) | every PR |

After all five sub-PRs (6a → 6e) merge and the verification matrix is green, **Atlas Step 6 is done**. Step 7 becomes unblocked.

## Implementation order recap

```
6a (PR) → 6b (PR) → 6c (PR) → 6d (PR) → 6e (PR — Step 6 complete)
```

Each PR is independently reviewable. No big-bang merge.

## Open at implementation time (carried forward from design §8)

Resolve via PR review or while implementing:

- **Eval prompt content** — actual prompt text for eval_security and eval_test. Draft in 6b; refine after first real run.
- **Demotion sweep cadence** — every REVIEWING for v3.0; could move to scheduled task in Phase 6 cognify.
- **applies_when widening heuristic** — keyword extraction in v3.0; smarter in Phase 3.x.
- **Eval timeout / API-quota handling** — graceful skip in v3.0; adaptive backoff in Phase 3.x.
- **`relevant_memory_id` extraction** — eval prompts must surface which memory in brief their finding maps to (or null). Prompt-engineering detail.
