# Atlas Step 5 — Curator Agent + Cascade Re-evaluation Queue Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task.

**Goal:** Ship Step 5 of Atlas / Phase 3: a curator agent + cascade re-evaluation queue that flips postulates P1 + P2 from `xfail(strict=True)` to passing, adds five new postulates, and produces a sectioned `CuratorBrief` consumed by future Step 6 eval agents and Phase 6 cognify.

**Architecture:** Hybrid trigger model. `store()` enqueues affected dependents into `curator_re_eval_queue`; a mechanical worker drains the queue (no LLM) using the bounded additive cascade penalty. `end_session()` accepts structured judgment from the calling agent (no separate LLM agent). Brief is a versioned Pydantic v1.0 model cached on `factory_jobs.curator_brief` JSONB.

**Tech stack:** Python 3.14, pytest, psycopg2, pgvector, Pydantic v2, TypeScript MCP server (Node 20), Postgres 16.

**Design reference:** `docs/plans/2026-05-04-step-5-curator-design.md` (locked).

**Worktree:** `/Users/patrickkelly/devbrain/.worktrees/atlas-step-5-curator` on branch `feat/atlas-step-5-curator`. Baseline 180 tests passing (CI no-DB subset).

**Conventions to know before starting:**
- Tests live in `factory/tests/` and run from `cd factory && pytest tests/...`
- Postgres tests use the `conn` fixture + `project_factory` + `memory_factory` from `tests/postulates/conftest.py`
- Postulates ship in `tests/postulates/test_pN_<slug>.py` (one file per postulate, `xfail(strict=True)` until the feature lands)
- Sequential numbered migrations in `migrations/0NN_<slug>.sql`. Step 5 uses **017** (last is 016)
- Pydantic v2 syntax (`model_validate`, `model_dump`, `Field`, `Annotated`)
- Commit prefix per `~/.claude/CLAUDE.md`: `feat:` for new user-facing, `fix:` for bugs, `chore:` for deps, `docs:` for docs, `test:` for tests, `refactor:` for restructuring

---

## Phase 5a — Migration + Pydantic types

**Files:**
- Create: `migrations/017_curator_queue_and_brief.sql`
- Create: `factory/curator/__init__.py`
- Create: `factory/curator/types.py`
- Test: `factory/tests/test_curator_types.py`
- Test: `factory/tests/test_migration_017.py`

### Task 5a-1: Write the migration

**Step 1: Create `migrations/017_curator_queue_and_brief.sql`:**

```sql
-- Atlas Step 5 — Curator agent + cascade re-evaluation queue
-- ============================================================================
--
-- Adds three substrate elements:
--   1. devbrain.curator_re_eval_queue — drained by the cascade worker. One
--      row per (dependent memory, source memory, edge type). Worker uses
--      SELECT ... FOR UPDATE SKIP LOCKED for safe concurrent drainage.
--   2. devbrain.memory.last_cascade_at — audit timestamp; set by the worker
--      every time it processes a row (whether or not strength changed).
--   3. devbrain.factory_jobs.curator_brief — JSONB snapshot of the brief
--      generated at QUEUED -> PLANNING. Every job phase reads the same
--      snapshot.
--
-- Foreign-key behavior: ON DELETE CASCADE for memory_id (if the dependent
-- memory is deleted, drop the queue row). For cascade_source_id we just
-- reference; the source could itself be archived but should still exist
-- as an audit anchor. If you need to delete a source row, drain the queue
-- first.

CREATE TABLE IF NOT EXISTS devbrain.curator_re_eval_queue (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id            UUID NOT NULL REFERENCES devbrain.memory(id) ON DELETE CASCADE,
    cascade_source_id    UUID NOT NULL REFERENCES devbrain.memory(id),
    edge_type            TEXT NOT NULL CHECK (edge_type IN
                            ('supersedes','archived_at','applies_when')),
    enqueued_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    attempt_count        INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT
);

-- FIFO index for drainage order. Workers SELECT ... ORDER BY enqueued_at LIMIT N.
CREATE INDEX IF NOT EXISTS idx_re_eval_queue_fifo
    ON devbrain.curator_re_eval_queue (enqueued_at);

-- Skip rows that have failed too many times (worker filters with WHERE attempt_count < 3)
CREATE INDEX IF NOT EXISTS idx_re_eval_queue_unfailed
    ON devbrain.curator_re_eval_queue (enqueued_at)
    WHERE attempt_count < 3;

-- Dedup active queue rows. The cascade penalty is additive (not idempotent), so
-- two simultaneous enqueues for the same (dependent, source, edge_type) triplet
-- would double-penalize. The enqueue path uses INSERT ... ON CONFLICT DO NOTHING
-- against this index. Failed rows (attempt_count = 3) don't block legitimate
-- re-enqueues after they're surfaced and triaged.
CREATE UNIQUE INDEX IF NOT EXISTS idx_re_eval_queue_dedup
    ON devbrain.curator_re_eval_queue (memory_id, cascade_source_id, edge_type)
    WHERE attempt_count < 3;

-- Audit: last time the cascade worker touched this memory row.
ALTER TABLE devbrain.memory
    ADD COLUMN IF NOT EXISTS last_cascade_at TIMESTAMPTZ;

-- Cached brief — every phase of a factory job reads from this column.
ALTER TABLE devbrain.factory_jobs
    ADD COLUMN IF NOT EXISTS curator_brief JSONB;
```

**Step 2: Run migration on local devbrain DB:**

```bash
cd /Users/patrickkelly/devbrain/.worktrees/atlas-step-5-curator
docker compose exec -T devbrain-db psql -U devbrain -d devbrain < migrations/017_curator_queue_and_brief.sql
```

Expected: `CREATE TABLE`, `CREATE INDEX`, `ALTER TABLE` (3x).

**Step 3: Verify schema:**

```bash
docker compose exec -T devbrain-db psql -U devbrain -d devbrain -c "\d devbrain.curator_re_eval_queue"
docker compose exec -T devbrain-db psql -U devbrain -d devbrain -c "\d devbrain.memory" | grep last_cascade_at
docker compose exec -T devbrain-db psql -U devbrain -d devbrain -c "\d devbrain.factory_jobs" | grep curator_brief
```

**Step 4: Commit:**

```bash
git add migrations/017_curator_queue_and_brief.sql
git commit -m "feat(memory): migration 017 — curator_re_eval_queue + last_cascade_at + curator_brief (Atlas Step 5a)"
```

### Task 5a-2: Test the migration applies cleanly to a fresh DB

**Files:** `factory/tests/test_migration_017.py`

**Step 1: Write the failing test:**

```python
"""Test migration 017 applies cleanly and creates expected schema objects."""
from __future__ import annotations

import pytest


@pytest.mark.db
def test_017_creates_curator_queue_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='devbrain' AND table_name='curator_re_eval_queue'"
        )
        assert cur.fetchone() is not None


@pytest.mark.db
def test_017_curator_queue_has_required_columns(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='devbrain' AND table_name='curator_re_eval_queue'"
        )
        cols = {row[0] for row in cur.fetchall()}
    assert {"id","memory_id","cascade_source_id","edge_type",
            "enqueued_at","attempt_count","last_error"} <= cols


@pytest.mark.db
def test_017_memory_has_last_cascade_at(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='devbrain' AND table_name='memory' "
            "AND column_name='last_cascade_at'"
        )
        assert cur.fetchone() is not None


@pytest.mark.db
def test_017_factory_jobs_has_curator_brief(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='devbrain' AND table_name='factory_jobs' "
            "AND column_name='curator_brief'"
        )
        assert cur.fetchone() is not None
```

**Step 2: Run, verify it passes (migration was applied in 5a-1):**

```bash
cd factory && /Users/patrickkelly/devbrain/.venv/bin/python -m pytest tests/test_migration_017.py -v
```

Expected: 4 passed.

**Step 3: Commit:**

```bash
git add factory/tests/test_migration_017.py
git commit -m "test(memory): migration 017 schema assertions (Atlas Step 5a)"
```

### Task 5a-3: Pydantic types — `factory/curator/types.py`

**Files:** `factory/curator/__init__.py`, `factory/curator/types.py`, `factory/tests/test_curator_types.py`

**Step 1: Create empty `__init__.py`:**

```bash
touch factory/curator/__init__.py
```

**Step 2: Write the failing test (`factory/tests/test_curator_types.py`):**

```python
"""Round-trip + validation tests for factory.curator.types."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from curator.types import (
    CascadeNote,
    CuratorBrief,
    MemoryRef,
)


def _ref(**overrides):
    base = dict(
        id=uuid4(),
        kind="decision",
        title="Use additive penalty",
        content_excerpt="Cascade penalty is bounded so multi-hop converges.",
        tier="memory",
        strength=Decimal("0.85"),
        last_cascade_at=None,
    )
    return MemoryRef(**{**base, **overrides})


def _note(**overrides):
    base = dict(
        affected_memory_id=uuid4(),
        cascade_source_id=uuid4(),
        edge_type="supersedes",
        occurred_at=datetime.now(timezone.utc),
        summary="Rule R6 was superseded 2h ago",
    )
    return CascadeNote(**{**base, **overrides})


def test_memory_ref_roundtrip():
    ref = _ref()
    serialized = ref.model_dump_json()
    restored = MemoryRef.model_validate_json(serialized)
    assert restored == ref


def test_cascade_note_roundtrip():
    note = _note()
    serialized = note.model_dump_json()
    restored = CascadeNote.model_validate_json(serialized)
    assert restored == note


def test_curator_brief_roundtrip():
    brief = CuratorBrief(
        version="1.0",
        job_id=uuid4(),
        project_id=uuid4(),
        rules=[_ref(tier="rule")],
        lessons=[_ref(tier="lesson")],
        relevant_decisions=[_ref()],
        recent_cascade_signals=[_note()],
        generated_at=datetime.now(timezone.utc),
    )
    restored = CuratorBrief.model_validate_json(brief.model_dump_json())
    assert restored == brief


def test_curator_brief_rejects_unknown_version():
    with pytest.raises(ValidationError):
        CuratorBrief(
            version="2.0",  # not a valid Literal value
            job_id=uuid4(),
            project_id=uuid4(),
            rules=[],
            lessons=[],
            relevant_decisions=[],
            recent_cascade_signals=[],
            generated_at=datetime.now(timezone.utc),
        )


def test_memory_ref_rejects_invalid_tier():
    with pytest.raises(ValidationError):
        _ref(tier="bogus")


def test_cascade_note_rejects_invalid_edge_type():
    with pytest.raises(ValidationError):
        _note(edge_type="not_an_edge")
```

**Step 3: Run, verify FAIL with `ImportError` or `ModuleNotFoundError`:**

```bash
cd factory && /Users/patrickkelly/devbrain/.venv/bin/python -m pytest tests/test_curator_types.py -v
```

Expected: ImportError on `curator.types`.

**Step 4: Implement `factory/curator/types.py`:**

```python
"""Versioned Pydantic models for the curator agent.

Stable contract consumed by Step 6 eval agents and (eventually) Phase 6
cognify. Bumping a model goes via Pydantic discriminated union on the
`version` Literal — adding "1.1" is a clean fork; old code keeps reading
v1.0 rows from factory_jobs.curator_brief unchanged.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class MemoryRef(BaseModel):
    """A single memory included in the curator brief."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    kind: Literal["chunk", "decision", "pattern", "issue", "session_summary"]
    title: str | None
    content_excerpt: str  # first ~500 chars of content
    tier: Literal["memory", "lesson", "rule"]
    strength: Decimal
    last_cascade_at: datetime | None


class CascadeNote(BaseModel):
    """Surfaces a recent cascade event the planner should be aware of."""

    model_config = ConfigDict(frozen=True)

    affected_memory_id: UUID
    cascade_source_id: UUID
    edge_type: Literal["supersedes", "archived_at", "applies_when"]
    occurred_at: datetime
    summary: str  # human-readable: "Rule R6 was superseded 2h ago"


class CuratorBrief(BaseModel):
    """The brief handed from the curator to a factory job's planner.

    Cached on `devbrain.factory_jobs.curator_brief` JSONB so every phase
    (planner, implementer, reviewer, QA) reads the identical snapshot.
    """

    model_config = ConfigDict(frozen=True)

    version: Literal["1.0"]
    job_id: UUID
    project_id: UUID
    rules: list[MemoryRef]              # tier='rule', compliance-profile-filtered
    lessons: list[MemoryRef]            # tier='lesson', strength-ranked
    relevant_decisions: list[MemoryRef] # tier='memory', applies_when matched
    recent_cascade_signals: list[CascadeNote]
    generated_at: datetime
```

**Step 5: Run, verify PASS:**

```bash
cd factory && /Users/patrickkelly/devbrain/.venv/bin/python -m pytest tests/test_curator_types.py -v
```

Expected: 6 passed.

**Step 6: Commit:**

```bash
git add factory/curator/__init__.py factory/curator/types.py factory/tests/test_curator_types.py
git commit -m "feat(curator): Pydantic v1.0 types — MemoryRef, CascadeNote, CuratorBrief (Atlas Step 5a)"
```

### Task 5a-4: Open PR for Phase 5a

```bash
git push -u origin feat/atlas-step-5-curator
gh pr create --title "feat(memory): Atlas Step 5a — migration 017 + curator types" --body "$(cat <<'EOF'
## Summary
- Migration 017: `curator_re_eval_queue` table, `memory.last_cascade_at` column, `factory_jobs.curator_brief` JSONB column
- `factory/curator/types.py` — versioned Pydantic v1.0 models (`MemoryRef`, `CascadeNote`, `CuratorBrief`)
- 4 schema-assertion tests + 6 type round-trip / validation tests

Part of Atlas Step 5 implementation. See:
- Design: `docs/plans/2026-05-04-step-5-curator-design.md`
- Plan: `docs/plans/2026-05-04-step-5-curator-implementation.md`

## Test plan
- [x] Migration applies cleanly: `\d devbrain.curator_re_eval_queue` shows expected columns
- [x] Existing 180 no-DB tests still pass
- [x] New schema assertion tests pass (4)
- [x] New Pydantic round-trip tests pass (6)

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

After merge → continue with Phase 5b on the same branch (rebase from main if needed).

---

## Phase 5b — `factory/curator/strength.py`

Pure functions, no DB. 100% coverage required.

**Files:**
- Create: `factory/curator/strength.py`
- Test: `factory/tests/test_curator_strength.py`

### Task 5b-1: Write failing test for `cascade_penalty()`

**File:** `factory/tests/test_curator_strength.py`

```python
"""Unit tests for curator strength formula (pure functions)."""
from __future__ import annotations

from decimal import Decimal

import pytest

from curator.strength import (
    PENALTY,
    apply_cascade,
    cascade_penalty,
    freshness_decay,
)


def test_penalty_constants_ordered():
    """supersedes > archived_at > applies_when."""
    assert PENALTY["supersedes"] > PENALTY["archived_at"] > PENALTY["applies_when"]


def test_freshness_decay_at_zero_seconds_is_one():
    assert freshness_decay(0) == 1.0


def test_freshness_decay_at_24h_is_half():
    assert freshness_decay(86400) == pytest.approx(0.5, rel=1e-9)


def test_freshness_decay_at_48h_is_quarter():
    assert freshness_decay(86400 * 2) == pytest.approx(0.25, rel=1e-9)


def test_cascade_penalty_zero_age_supersedes():
    p = cascade_penalty("supersedes", 0)
    assert p == Decimal(str(PENALTY["supersedes"]))


def test_cascade_penalty_decays_with_age():
    p_now = cascade_penalty("supersedes", 0)
    p_24h = cascade_penalty("supersedes", 86400)
    assert p_24h < p_now
    assert p_24h == pytest.approx(p_now / 2, rel=1e-6)


def test_cascade_penalty_unknown_edge_type_raises():
    with pytest.raises(KeyError):
        cascade_penalty("not_an_edge", 0)


def test_apply_cascade_subtracts_penalty():
    new = apply_cascade(Decimal("0.85"), "supersedes", 0)
    assert new == Decimal("0.85") - cascade_penalty("supersedes", 0)


def test_apply_cascade_clamped_at_zero():
    new = apply_cascade(Decimal("0.10"), "supersedes", 0)
    assert new == Decimal("0")
    assert new >= Decimal("0")  # never negative


def test_apply_cascade_strong_memory_survives():
    # 0.85 strength, applies_when (lightest) cascade — should retain meaningful strength
    new = apply_cascade(Decimal("0.85"), "applies_when", 0)
    assert new > Decimal("0.5")
```

**Run, verify FAIL** (ImportError):

```bash
cd factory && /Users/patrickkelly/devbrain/.venv/bin/python -m pytest tests/test_curator_strength.py -v
```

### Task 5b-2: Implement `factory/curator/strength.py`

```python
"""Cascade strength formula — pure functions, no DB dependency.

Forward-compat: callable from Phase 6 cognify offline for batch reweighting
(playbook §9). Do NOT add DB calls here. Do NOT add LLM calls here.

The formula:
    new_strength = max(0, old_strength - cascade_penalty(edge_type, age_seconds))

where cascade_penalty is the per-edge-type base penalty modulated by a 24h
half-life freshness decay. Bounded subtraction keeps multi-hop cascades
from compounding to zero through dep depth alone — a 4-hop cascade
through bounded penalties stays sensible.
"""
from __future__ import annotations

from decimal import Decimal

# Per-edge-type base penalty. Tuned conservatively — penalties are deliberately
# small so a single cascade rarely zeroes out a memory. The end_session
# judgment agent provides additional adjustments on top.
PENALTY: dict[str, float] = {
    "supersedes": 0.40,    # upstream replaced — strongest signal
    "archived_at": 0.25,   # upstream archived — moderate
    "applies_when": 0.10,  # upstream's context changed — light touch
}


def freshness_decay(age_seconds: float) -> float:
    """Penalty fades with time. Half-life = 24 hours.

    At 0s: 1.0 (full penalty)
    At 24h: 0.5 (half penalty)
    At 48h: 0.25
    At 1 week: ~0.0014
    """
    return 0.5 ** (age_seconds / 86400)


def cascade_penalty(edge_type: str, age_seconds: float) -> Decimal:
    """Base penalty modulated by freshness decay.

    Raises KeyError if edge_type is not in PENALTY.
    """
    base = PENALTY[edge_type]
    return Decimal(str(base * freshness_decay(age_seconds)))


def apply_cascade(
    strength: Decimal, edge_type: str, age_seconds: float
) -> Decimal:
    """Apply a cascade penalty, clamped at zero.

    Pure function — call this from anywhere (worker, cognify, postulate test).
    """
    new = strength - cascade_penalty(edge_type, age_seconds)
    return max(Decimal("0"), new)
```

**Run, verify PASS:**

```bash
cd factory && /Users/patrickkelly/devbrain/.venv/bin/python -m pytest tests/test_curator_strength.py -v --cov=curator.strength --cov-report=term-missing
```

Expected: 10 passed, 100% coverage on `curator/strength.py`.

### Task 5b-3: Commit + open PR for 5b

```bash
git add factory/curator/strength.py factory/tests/test_curator_strength.py
git commit -m "feat(curator): bounded additive cascade penalty + 24h freshness decay (Atlas Step 5b)"
git push
gh pr create --title "feat(curator): Atlas Step 5b — strength formula" --body "Pure-function cascade penalty formula. 100% coverage. Forward-compat with Phase 6 cognify offline reweighting (callable without DB)."
```

---

## Phase 5c — `factory/curator/worker.py` (queue drainer)

**Files:**
- Create: `factory/curator/worker.py`
- Modify: `factory/orchestrator.py` (add `drain_curator_queue()` to main poll loop)
- Test: `factory/tests/test_curator_worker.py`
- Postulate: `tests/postulates/test_p_cycle.py`
- Postulate: `tests/postulates/test_p_archived_mid_cascade.py`
- Postulate: `tests/postulates/test_p_stuck_surface_able.py`
- New CLI subcommand: `factory/cli.py` — add `curator queue-stuck` (Step 5c-7)

### Task 5c-1: Write failing integration test for `drain_one_batch()`

**File:** `factory/tests/test_curator_worker.py`

```python
"""Integration tests for the cascade worker."""
from __future__ import annotations

import pytest

from curator.worker import drain_one_batch
from curator.strength import PENALTY


@pytest.mark.db
def test_drain_one_batch_processes_single_row(
    conn, project_factory, memory_factory
):
    project = project_factory("dwc")
    m_old = memory_factory(project["id"], kind="pattern", content="old")
    m_dep = memory_factory(project["id"], kind="issue", content="dep")

    # Set up: m_dep depends on m_old, with starting strength 0.85
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET strength = 0.85 WHERE id = %s",
            (m_dep["id"],),
        )
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'test')",
            (m_dep["id"], m_old["id"]),
        )
        cur.execute(
            "INSERT INTO devbrain.curator_re_eval_queue "
            "(memory_id, cascade_source_id, edge_type) VALUES (%s, %s, %s)",
            (m_dep["id"], m_old["id"], "supersedes"),
        )
    conn.commit()

    drained = drain_one_batch(conn, batch_size=10)
    assert drained == 1

    # m_dep strength should drop ~0.40 (supersedes penalty, ~0 age)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT strength, last_cascade_at FROM devbrain.memory WHERE id=%s",
            (m_dep["id"],),
        )
        strength, last_cascade = cur.fetchone()
    assert float(strength) == pytest.approx(0.45, abs=0.01)
    assert last_cascade is not None

    # Queue row should be deleted
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.curator_re_eval_queue WHERE memory_id=%s",
            (m_dep["id"],),
        )
        assert cur.fetchone()[0] == 0


@pytest.mark.db
def test_drain_skips_archived_target(conn, project_factory, memory_factory):
    project = project_factory("dst")
    m_dep = memory_factory(project["id"], content="will be archived")
    m_src = memory_factory(project["id"], content="source")

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET archived_at = NOW(), strength = 0.7 "
            "WHERE id = %s",
            (m_dep["id"],),
        )
        cur.execute(
            "INSERT INTO devbrain.curator_re_eval_queue "
            "(memory_id, cascade_source_id, edge_type) VALUES (%s, %s, 'supersedes')",
            (m_dep["id"], m_src["id"]),
        )
    conn.commit()

    drained = drain_one_batch(conn, batch_size=10)
    assert drained == 1

    # Strength NOT updated (archived row left alone)
    with conn.cursor() as cur:
        cur.execute("SELECT strength FROM devbrain.memory WHERE id=%s", (m_dep["id"],))
        assert float(cur.fetchone()[0]) == 0.7

    # Queue row still deleted
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.curator_re_eval_queue WHERE memory_id=%s",
            (m_dep["id"],),
        )
        assert cur.fetchone()[0] == 0


@pytest.mark.db
def test_drain_multi_hop_enqueues_dependents(
    conn, project_factory, memory_factory
):
    project = project_factory("dmh")
    a = memory_factory(project["id"], content="a")
    b = memory_factory(project["id"], content="b")
    c = memory_factory(project["id"], content="c")

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET strength = 0.85 WHERE id IN (%s,%s,%s)",
            (a["id"], b["id"], c["id"]),
        )
        # b depends_on a, c depends_on b
        cur.executemany(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'test')",
            [(b["id"], a["id"]), (c["id"], b["id"])],
        )
        cur.execute(
            "INSERT INTO devbrain.curator_re_eval_queue "
            "(memory_id, cascade_source_id, edge_type) VALUES (%s, %s, 'supersedes')",
            (b["id"], a["id"]),
        )
    conn.commit()

    drain_one_batch(conn, batch_size=10)

    # b should be processed; c should be ENQUEUED (multi-hop)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.curator_re_eval_queue WHERE memory_id=%s",
            (c["id"],),
        )
        assert cur.fetchone()[0] == 1


@pytest.mark.db
def test_drain_increments_attempt_count_on_failure(
    conn, project_factory, memory_factory, monkeypatch
):
    project = project_factory("daf")
    m = memory_factory(project["id"], content="will fail")
    src = memory_factory(project["id"], content="src")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.curator_re_eval_queue "
            "(memory_id, cascade_source_id, edge_type) VALUES (%s, %s, 'supersedes') "
            "RETURNING id",
            (m["id"], src["id"]),
        )
        queue_id = cur.fetchone()[0]
    conn.commit()

    # Force apply_cascade to raise
    from curator import worker as worker_mod
    monkeypatch.setattr(
        worker_mod, "apply_cascade", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    drained = drain_one_batch(conn, batch_size=10)
    assert drained == 0  # nothing successfully drained

    with conn.cursor() as cur:
        cur.execute(
            "SELECT attempt_count, last_error FROM devbrain.curator_re_eval_queue "
            "WHERE id=%s",
            (queue_id,),
        )
        attempt_count, last_error = cur.fetchone()
    assert attempt_count == 1
    assert "boom" in (last_error or "")
```

**Run, verify FAIL** (ImportError on `curator.worker`).

### Task 5c-2: Implement `factory/curator/worker.py`

```python
"""Cascade re-evaluation queue drainer.

Runs in the existing factory orchestrator process — no new daemon. Adds one
sibling poll alongside the existing factory_jobs poll.

Each batch:
1. SELECT ... FOR UPDATE SKIP LOCKED claims up to batch_size rows
2. For each: load memory, compute new_strength, UPDATE memory, DELETE queue row
3. Multi-hop: if penalty was significant (> 0.05 after freshness decay),
   walk the row's own dependents and enqueue them

On exception: increment attempt_count, persist last_error, leave queue row.
After 3 failures, row is skipped (idx_re_eval_queue_unfailed filters).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from curator.strength import apply_cascade, cascade_penalty

logger = logging.getLogger(__name__)

# Multi-hop propagation threshold. If a cascade's penalty (after freshness
# decay) is smaller than this, don't bother propagating to the dependent's
# own dependents — too small to matter.
MULTI_HOP_THRESHOLD = Decimal("0.05")


def drain_one_batch(conn: Any, batch_size: int = 50) -> int:
    """Drain up to batch_size rows from curator_re_eval_queue.

    Returns the number of rows successfully drained (queue rows DELETEd).
    Failed rows stay in the queue with attempt_count incremented.
    """
    drained = 0
    with conn.cursor() as cur:
        # Claim a batch with SKIP LOCKED — multiple workers safe.
        cur.execute(
            """
            SELECT id, memory_id, cascade_source_id, edge_type, enqueued_at,
                   attempt_count
            FROM devbrain.curator_re_eval_queue
            WHERE attempt_count < 3
            ORDER BY enqueued_at
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (batch_size,),
        )
        rows = cur.fetchall()

    for row in rows:
        queue_id, memory_id, source_id, edge_type, enqueued_at, attempt_count = row
        try:
            _process_one(conn, queue_id, memory_id, source_id, edge_type, enqueued_at)
            drained += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("drain_one_batch: row %s failed", queue_id)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE devbrain.curator_re_eval_queue "
                    "SET attempt_count = attempt_count + 1, last_error = %s "
                    "WHERE id = %s",
                    (str(exc)[:1000], queue_id),
                )
            conn.commit()

    return drained


def _process_one(
    conn: Any,
    queue_id: Any,
    memory_id: Any,
    source_id: Any,
    edge_type: str,
    enqueued_at: datetime,
) -> None:
    """Process a single queue row in its own transaction."""
    with conn.cursor() as cur:
        # Load target memory.
        cur.execute(
            "SELECT strength, archived_at, last_cascade_at "
            "FROM devbrain.memory WHERE id = %s",
            (memory_id,),
        )
        result = cur.fetchone()
        if result is None:
            # Memory deleted — drop queue row.
            cur.execute(
                "DELETE FROM devbrain.curator_re_eval_queue WHERE id = %s",
                (queue_id,),
            )
            conn.commit()
            return
        strength, archived_at, last_cascade_at = result

        # Cycle prevention: skip if already cascaded since this source's mutation.
        if last_cascade_at is not None and last_cascade_at >= enqueued_at:
            cur.execute(
                "DELETE FROM devbrain.curator_re_eval_queue WHERE id = %s",
                (queue_id,),
            )
            conn.commit()
            return

        # Archived — just drop the queue row, don't update strength,
        # don't propagate to dependents.
        if archived_at is not None:
            cur.execute(
                "DELETE FROM devbrain.curator_re_eval_queue WHERE id = %s",
                (queue_id,),
            )
            conn.commit()
            return

        age_seconds = (datetime.now(timezone.utc) - enqueued_at).total_seconds()
        new_strength = apply_cascade(strength, edge_type, age_seconds)
        penalty = cascade_penalty(edge_type, age_seconds)

        cur.execute(
            "UPDATE devbrain.memory "
            "SET strength = %s, last_cascade_at = NOW() "
            "WHERE id = %s",
            (new_strength, memory_id),
        )
        cur.execute(
            "DELETE FROM devbrain.curator_re_eval_queue WHERE id = %s",
            (queue_id,),
        )

        # Multi-hop propagation.
        if penalty > MULTI_HOP_THRESHOLD:
            cur.execute(
                "INSERT INTO devbrain.curator_re_eval_queue "
                "(memory_id, cascade_source_id, edge_type) "
                "SELECT from_memory_id, %s, %s "
                "FROM devbrain.memory_dependencies "
                "WHERE to_memory_id = %s AND edge_type = 'depends_on'",
                (memory_id, edge_type, memory_id),
            )

    conn.commit()
```

**Run, verify PASS:**

```bash
cd factory && /Users/patrickkelly/devbrain/.venv/bin/python -m pytest tests/test_curator_worker.py -v
```

Expected: 4 passed.

### Task 5c-3: Wire `drain_curator_queue()` into the factory orchestrator main loop

**Modify:** `factory/orchestrator.py` (find the main poll loop)

Locate the polling loop (search for `poll_interval` or the `while True:` loop in orchestrator). Add a sibling call:

```python
from curator.worker import drain_one_batch as drain_curator_queue

# Inside main loop, after the existing factory_jobs poll:
try:
    drained = drain_curator_queue(conn, batch_size=50)
    if drained:
        logger.info("curator: drained %d queue rows", drained)
except Exception:
    logger.exception("curator: drain failed")
```

Run baseline tests + new ones:

```bash
cd factory && /Users/patrickkelly/devbrain/.venv/bin/python -m pytest tests/ -v --co -q 2>&1 | tail -3
```

Confirm orchestrator tests still pass.

### Task 5c-4: Postulate P_cycle

**File:** `tests/postulates/test_p_cycle.py`

```python
"""P_cycle — dependency cycle (A→B→A) converges in one wave.

POSTULATE
---------
If A depends_on B and B depends_on A (cycle), a cascade triggered by
mutating A converges in a single drain pass — neither row is processed
more than once per cascade source.
"""
from __future__ import annotations

import pytest

from curator.worker import drain_one_batch


def test_dependency_cycle_converges(conn, project_factory, memory_factory):
    project = project_factory("p_cycle")
    a = memory_factory(project["id"], content="a")
    b = memory_factory(project["id"], content="b")

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET strength = 0.9 WHERE id IN (%s, %s)",
            (a["id"], b["id"]),
        )
        cur.executemany(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'test')",
            [(a["id"], b["id"]), (b["id"], a["id"])],  # cycle
        )
        cur.execute(
            "INSERT INTO devbrain.curator_re_eval_queue "
            "(memory_id, cascade_source_id, edge_type) VALUES (%s, %s, 'supersedes')",
            (a["id"], b["id"]),
        )
    conn.commit()

    # Drain repeatedly — should converge in finite passes.
    iterations = 0
    while iterations < 5:
        drained = drain_one_batch(conn, batch_size=50)
        if drained == 0:
            break
        iterations += 1
    assert iterations < 5, "cycle did not converge — possible infinite loop"

    # Queue should be empty.
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM devbrain.curator_re_eval_queue")
        assert cur.fetchone()[0] == 0
```

### Task 5c-5: Postulate P_archived_mid_cascade

**File:** `tests/postulates/test_p_archived_mid_cascade.py`

```python
"""P_archived_mid_cascade — archived target during drain → DELETE without propagating.

POSTULATE
---------
If a memory is archived after being enqueued for re-eval but before the
worker drains it, the worker DELETEs the queue row, does NOT update
strength, and does NOT propagate to the row's dependents.
"""
from __future__ import annotations

import pytest

from curator.worker import drain_one_batch


def test_archived_target_no_propagation(conn, project_factory, memory_factory):
    project = project_factory("p_arch")
    a = memory_factory(project["id"], content="a")
    b = memory_factory(project["id"], content="b — depends on a, will archive")
    c = memory_factory(project["id"], content="c — depends on b, should NOT be enqueued")

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET strength = 0.85 WHERE id IN (%s,%s,%s)",
            (a["id"], b["id"], c["id"]),
        )
        cur.executemany(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type, created_by) "
            "VALUES (%s, %s, 'depends_on', 'test')",
            [(b["id"], a["id"]), (c["id"], b["id"])],
        )
        cur.execute(
            "INSERT INTO devbrain.curator_re_eval_queue "
            "(memory_id, cascade_source_id, edge_type) VALUES (%s, %s, 'supersedes')",
            (b["id"], a["id"]),
        )
        # Archive b BEFORE worker drains.
        cur.execute(
            "UPDATE devbrain.memory SET archived_at = NOW() WHERE id = %s",
            (b["id"],),
        )
    conn.commit()

    drain_one_batch(conn, batch_size=50)

    # c must NOT be enqueued.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.curator_re_eval_queue WHERE memory_id=%s",
            (c["id"],),
        )
        assert cur.fetchone()[0] == 0

    # b's strength NOT mutated.
    with conn.cursor() as cur:
        cur.execute("SELECT strength FROM devbrain.memory WHERE id=%s", (b["id"],))
        assert float(cur.fetchone()[0]) == 0.85
```

### Task 5c-6: Postulate P_stuck_surface_able

**File:** `tests/postulates/test_p_stuck_surface_able.py`

```python
"""P_stuck_surface_able — queue rows with attempt_count >= 3 surface in CLI.

POSTULATE
---------
A queue row that has failed 3+ times is reported by `devbrain curator queue-stuck`
with its memory_id, cascade_source_id, edge_type, attempt_count, and last_error.
"""
from __future__ import annotations

import pytest

from curator.cli import list_stuck_queue_rows  # implemented in 5c-7


def test_stuck_rows_listed(conn, project_factory, memory_factory):
    project = project_factory("p_stuck")
    m = memory_factory(project["id"], content="will fail")
    src = memory_factory(project["id"], content="src")

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.curator_re_eval_queue "
            "(memory_id, cascade_source_id, edge_type, attempt_count, last_error) "
            "VALUES (%s, %s, 'supersedes', 3, 'simulated failure')",
            (m["id"], src["id"]),
        )
    conn.commit()

    stuck = list_stuck_queue_rows(conn)
    assert len(stuck) == 1
    assert stuck[0]["memory_id"] == m["id"]
    assert stuck[0]["attempt_count"] == 3
    assert stuck[0]["last_error"] == "simulated failure"
```

### Task 5c-7: Implement `factory/curator/cli.py` for stuck-queue listing + register subcommand

**Create:** `factory/curator/cli.py`

```python
"""CLI surface for curator operations."""
from __future__ import annotations

from typing import Any


def list_stuck_queue_rows(conn: Any) -> list[dict]:
    """Return queue rows that have failed 3+ times.

    Used by `devbrain curator queue-stuck` and the P_stuck postulate.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, memory_id, cascade_source_id, edge_type,
                   enqueued_at, attempt_count, last_error
            FROM devbrain.curator_re_eval_queue
            WHERE attempt_count >= 3
            ORDER BY enqueued_at
            """
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
```

**Modify:** `factory/cli.py` — add a `curator` Click subcommand group + `queue-stuck`. Locate the existing `cli` group and add:

```python
@cli.group()
def curator():
    """Curator agent + cascade queue operations."""

@curator.command("queue-stuck")
def cmd_queue_stuck():
    """List re-eval queue rows that failed 3+ times."""
    from curator.cli import list_stuck_queue_rows
    from db import connect

    with connect() as conn:
        rows = list_stuck_queue_rows(conn)
    if not rows:
        click.echo("No stuck rows.")
        return
    for r in rows:
        click.echo(
            f"{r['id']}  memory={r['memory_id']}  edge={r['edge_type']}  "
            f"attempts={r['attempt_count']}  err={r['last_error']!r}"
        )
```

(Adjust the `connect` import to match the project's existing DB helper.)

**Run all P_* postulates + worker tests:**

```bash
cd factory && /Users/patrickkelly/devbrain/.venv/bin/python -m pytest tests/test_curator_worker.py tests/postulates/test_p_cycle.py tests/postulates/test_p_archived_mid_cascade.py tests/postulates/test_p_stuck_surface_able.py -v
```

Expected: all green.

### Task 5c-8: Commit + push 5c

```bash
git add factory/curator/worker.py factory/curator/cli.py factory/orchestrator.py factory/cli.py factory/tests/test_curator_worker.py tests/postulates/test_p_cycle.py tests/postulates/test_p_archived_mid_cascade.py tests/postulates/test_p_stuck_surface_able.py
git commit -m "feat(curator): cascade worker + multi-hop propagation + 3 postulates (Atlas Step 5c)"
git push
gh pr create --title "feat(curator): Atlas Step 5c — cascade worker + 3 postulates" --body "Worker drains curator_re_eval_queue using SELECT FOR UPDATE SKIP LOCKED. Multi-hop propagates beyond 0.05 penalty threshold. Archived targets drop without propagating. P_cycle, P_archived_mid_cascade, P_stuck_surface_able postulates pass."
```

---

## Phase 5d — `factory/curator/brief.py` + state machine integration

**Files:**
- Create: `factory/curator/brief.py`
- Modify: `factory/state_machine.py` (call `generate_brief()` on QUEUED→PLANNING)
- Test: `factory/tests/test_curator_brief.py`
- Postulate (flip): `tests/postulates/test_p1_supersession_cascades.py` (remove xfail)
- Postulate (flip): `tests/postulates/test_p2_archived_excluded.py` (remove xfail)

### Task 5d-1: Write failing test for `generate_brief()`

**File:** `factory/tests/test_curator_brief.py`

```python
"""Integration tests for brief generation."""
from __future__ import annotations

import pytest

from curator.brief import generate_brief


@pytest.mark.db
def test_brief_filters_by_compliance_profiles(
    conn, project_factory, memory_factory, factory_job_factory
):
    project = project_factory("bf", compliance_profiles_enabled=["hipaa"])
    rule_hipaa = memory_factory(
        project["id"], kind="decision", tier="rule",
        content="HIPAA rule", compliance_profiles=["hipaa"],
    )
    rule_soc2 = memory_factory(
        project["id"], kind="decision", tier="rule",
        content="SOC2 rule", compliance_profiles=["soc2"],
    )
    job = factory_job_factory(project["id"], spec="touches phi_log.py")

    brief = generate_brief(conn, job["id"], project["id"], job["spec"])

    rule_ids = {r.id for r in brief.rules}
    assert rule_hipaa["id"] in rule_ids
    assert rule_soc2["id"] not in rule_ids


@pytest.mark.db
def test_brief_excludes_archived(
    conn, project_factory, memory_factory, factory_job_factory
):
    project = project_factory("bea")
    m = memory_factory(project["id"], tier="lesson", content="archived me")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET archived_at = NOW() WHERE id = %s",
            (m["id"],),
        )
    conn.commit()
    job = factory_job_factory(project["id"], spec="anything")

    brief = generate_brief(conn, job["id"], project["id"], job["spec"])
    assert m["id"] not in {r.id for r in brief.lessons}


@pytest.mark.db
def test_brief_persisted_to_factory_job(
    conn, project_factory, factory_job_factory
):
    project = project_factory("bp")
    job = factory_job_factory(project["id"], spec="x")

    brief = generate_brief(conn, job["id"], project["id"], job["spec"])

    with conn.cursor() as cur:
        cur.execute(
            "SELECT curator_brief FROM devbrain.factory_jobs WHERE id = %s",
            (job["id"],),
        )
        stored = cur.fetchone()[0]
    assert stored is not None
    assert stored["version"] == "1.0"
    assert stored["job_id"] == str(brief.job_id)


@pytest.mark.db
def test_brief_includes_recent_cascade_signals(
    conn, project_factory, memory_factory, factory_job_factory
):
    project = project_factory("brc")
    m_dep = memory_factory(project["id"], tier="lesson", content="recently cascaded")
    m_src = memory_factory(project["id"], content="source")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET last_cascade_at = NOW() WHERE id = %s",
            (m_dep["id"],),
        )
    conn.commit()
    job = factory_job_factory(project["id"], spec="x")

    brief = generate_brief(conn, job["id"], project["id"], job["spec"])
    affected = {n.affected_memory_id for n in brief.recent_cascade_signals}
    assert m_dep["id"] in affected
```

**Note:** `compliance_profiles` and `compliance_profiles_enabled` columns ship in Step 7. For Step 5d, they may not exist yet. Two options:

1. **Defer profile filtering tests** — comment out `test_brief_filters_by_compliance_profiles` with a `# Step 7 dependency` note, ship the rest.
2. **Add the column in 5d** — out of scope per playbook §5 ("Step 7 ships compliance_profiles columns"). Rejected.

Going with option 1: implement `generate_brief()` to gracefully handle missing profile columns — load all `tier='rule'` rows when columns absent. Re-enable the test in Step 7.

`factory_job_factory` may not exist as a fixture yet. If it doesn't:

```python
# Add to tests/postulates/conftest.py:
@pytest.fixture
def factory_job_factory(conn):
    created = []
    def _make(project_id, spec="test", status="queued"):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO devbrain.factory_jobs (project_id, spec, status) "
                "VALUES (%s, %s, %s) RETURNING id, project_id, spec, status",
                (project_id, spec, status),
            )
            cols = [d[0] for d in cur.description]
            row = dict(zip(cols, cur.fetchone()))
        conn.commit()
        created.append(row["id"])
        return row
    yield _make
    # Cleanup
    if created:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM devbrain.factory_jobs WHERE id = ANY(%s)",
                (created,),
            )
        conn.commit()
```

### Task 5d-2: Implement `factory/curator/brief.py`

```python
"""Curator brief generator.

Synchronous; called from state_machine.transition_queued_to_planning(). Pure
filtering + ranking — no LLM in v3.0. Could become LLM-driven later
without breaking the CuratorBrief v1.0 contract.

Profile filtering depends on the compliance_profiles columns shipped in
Step 7. Until then, all tier='rule' rows are loaded (no profile filter).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from curator.types import CascadeNote, CuratorBrief, MemoryRef

LESSON_TOP_N = 20
CASCADE_SIGNAL_WINDOW = "24 hours"


def generate_brief(
    conn: Any, job_id: UUID, project_id: UUID, spec: str
) -> CuratorBrief:
    """Generate a CuratorBrief and persist to factory_jobs.curator_brief."""
    profiles = _load_enabled_profiles(conn, project_id)
    rules = _load_rules(conn, project_id, profiles)
    lessons = _load_lessons(conn, project_id, top_n=LESSON_TOP_N)
    decisions = _load_decisions_matching(conn, project_id, spec)
    cascades = _load_recent_cascades(conn, project_id)

    brief = CuratorBrief(
        version="1.0",
        job_id=job_id,
        project_id=project_id,
        rules=rules,
        lessons=lessons,
        relevant_decisions=decisions,
        recent_cascade_signals=cascades,
        generated_at=datetime.now(timezone.utc),
    )

    _persist_to_job(conn, job_id, brief)
    return brief


def _load_enabled_profiles(conn, project_id) -> list[str]:
    """Step 7 column. Returns [] if column doesn't exist yet."""
    with conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT compliance_profiles_enabled FROM devbrain.projects "
                "WHERE id = %s",
                (project_id,),
            )
            row = cur.fetchone()
            return list(row[0] or []) if row else []
        except Exception:
            conn.rollback()
            return []


def _load_rules(conn, project_id, profiles) -> list[MemoryRef]:
    """Filter by profile intersection if Step 7 column exists; else all rules."""
    base = (
        "SELECT id, kind, title, content, tier, strength, last_cascade_at "
        "FROM devbrain.memory "
        "WHERE project_id = %s AND tier = 'rule' AND archived_at IS NULL"
    )
    with conn.cursor() as cur:
        try:
            if profiles:
                cur.execute(
                    base + " AND compliance_profiles && %s ORDER BY strength DESC",
                    (project_id, profiles),
                )
            else:
                cur.execute(base + " ORDER BY strength DESC", (project_id,))
        except Exception:
            conn.rollback()
            cur.execute(base + " ORDER BY strength DESC", (project_id,))
        return [_to_ref(row) for row in cur.fetchall()]


def _load_lessons(conn, project_id, top_n) -> list[MemoryRef]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, kind, title, content, tier, strength, last_cascade_at "
            "FROM devbrain.memory "
            "WHERE project_id = %s AND tier = 'lesson' AND archived_at IS NULL "
            "ORDER BY strength DESC LIMIT %s",
            (project_id, top_n),
        )
        return [_to_ref(row) for row in cur.fetchall()]


def _load_decisions_matching(conn, project_id, spec) -> list[MemoryRef]:
    """Naive matcher v0.1 — substring match against spec text.

    TODO Phase 3.x: smarter matcher (semantic similarity, structured
    applies_when match). Don't change the function signature.
    """
    keywords = [w for w in spec.split() if len(w) > 3][:10]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, kind, title, content, tier, strength, last_cascade_at "
            "FROM devbrain.memory "
            "WHERE project_id = %s AND tier = 'memory' AND archived_at IS NULL "
            "AND (%s::text[] IS NULL OR content ILIKE ANY(%s)) "
            "ORDER BY strength DESC LIMIT 30",
            (project_id, keywords or None,
             [f"%{w}%" for w in keywords] if keywords else ["%"]),
        )
        return [_to_ref(row) for row in cur.fetchall()]


def _load_recent_cascades(conn, project_id) -> list[CascadeNote]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, last_cascade_at
            FROM devbrain.memory
            WHERE project_id = %s
              AND last_cascade_at >= NOW() - INTERVAL '{CASCADE_SIGNAL_WINDOW}'
              AND archived_at IS NULL
            ORDER BY last_cascade_at DESC
            LIMIT 20
            """,
            (project_id,),
        )
        notes = []
        for memory_id, occurred_at in cur.fetchall():
            notes.append(CascadeNote(
                affected_memory_id=memory_id,
                cascade_source_id=memory_id,  # placeholder — could trace from ledger later
                edge_type="supersedes",  # placeholder — refine when ledger lookup ships
                occurred_at=occurred_at,
                summary=f"Memory {memory_id} re-evaluated by cascade",
            ))
        return notes


def _to_ref(row) -> MemoryRef:
    mid, kind, title, content, tier, strength, last_cascade_at = row
    return MemoryRef(
        id=mid,
        kind=kind,
        title=title,
        content_excerpt=(content or "")[:500],
        tier=tier,
        strength=strength,
        last_cascade_at=last_cascade_at,
    )


def _persist_to_job(conn, job_id, brief: CuratorBrief) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.factory_jobs SET curator_brief = %s::jsonb "
            "WHERE id = %s",
            (brief.model_dump_json(), job_id),
        )
    conn.commit()
```

### Task 5d-3: Wire `generate_brief()` into the QUEUED→PLANNING transition

**Modify:** `factory/state_machine.py`. Locate the `transition()` method and intercept QUEUED→PLANNING:

```python
def transition(self, job_id: str, new_status: JobStatus, **updates) -> FactoryJob:
    job = self.get_job(job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found")
    # ... existing legality validation ...

    # NEW — generate curator brief at QUEUED -> PLANNING.
    if (
        JobStatus(job.status) == JobStatus.QUEUED
        and new_status == JobStatus.PLANNING
    ):
        try:
            from curator.brief import generate_brief
            generate_brief(self.conn, job.id, job.project_id, job.spec)
        except Exception as exc:
            # Don't lose the job — record error and BLOCK after 3 attempts.
            self._record_brief_failure(job_id, str(exc))
            raise

    # ... existing transition logic ...
```

Add `_record_brief_failure()` helper that increments `error_count`, writes `last_error`, and transitions to `BLOCKED` after 3 attempts.

### Task 5d-4: Flip P1 + P2 from xfail to passing

**Modify:** `tests/postulates/test_p1_supersession_cascades.py` and `tests/postulates/test_p2_archived_excluded.py`. Remove the `@pytest.mark.xfail(strict=True, ...)` decorator and any "expected failure" wording in the docstring. Add a comment:

```python
# Activated in Atlas Step 5d (PR #<num>). Was xfail(strict=True) until the
# curator agent landed. See docs/plans/2026-05-04-step-5-curator-design.md.
```

Run them — they should now PASS:

```bash
cd factory && /Users/patrickkelly/devbrain/.venv/bin/python -m pytest tests/postulates/test_p1_supersession_cascades.py tests/postulates/test_p2_archived_excluded.py -v
```

Expected: 2 passed.

### Task 5d-5: Run full postulate suite + brief tests

```bash
cd factory && /Users/patrickkelly/devbrain/.venv/bin/python -m pytest tests/postulates/ tests/test_curator_brief.py -v
```

Expected: all postulates green (P1, P2, P3, P_cycle, P_archived_mid_cascade, P_stuck_surface_able) + 4 brief tests.

### Task 5d-6: Commit + PR for 5d

```bash
git add factory/curator/brief.py factory/state_machine.py factory/tests/test_curator_brief.py tests/postulates/test_p1_supersession_cascades.py tests/postulates/test_p2_archived_excluded.py
git commit -m "feat(curator): brief generator + state machine integration; flip P1+P2 (Atlas Step 5d)"
git push
gh pr create --title "feat(curator): Atlas Step 5d — brief generator + flip P1+P2" --body "Brief generated synchronously at QUEUED→PLANNING, persisted to factory_jobs.curator_brief JSONB. P1 (supersession cascades) and P2 (archived excluded) flip from xfail(strict=True) to passing. Brief naive applies_when matcher is substring-based; smarter matcher deferred to Phase 3.x."
```

---

## Phase 5e — `store()` cascade enqueue + `end_session()` enrichment + drain trigger

> **Three concerns in one phase, all in the MCP server layer:**
>
> 1. **`store()` cascade detection + enqueue** — when an agent calls `store()`
>    with a cascading mutation (writes a `supersedes` edge, sets `archived_at`,
>    or mutates `applies_when`), detect affected dependents from
>    `memory_dependencies` and INSERT into `devbrain.curator_re_eval_queue`.
>    Without this, the queue is always empty in normal operation.
> 2. **`end_session()` enrichment** — accepts `cascade_decisions`,
>    `new_relationships`, `lesson_candidates` from the calling agent.
> 3. **`end_session()` drain trigger** — after applying judgment from
>    enrichment payload, drain the queue. Honors the design promise that
>    ripple effects propagate from anywhere (chat sessions + factory jobs),
>    not just at factory job startup.
>
> **Architecture (per locked design + Patrick's option E refinement on 2026-05-04):**
>
> | Trigger | When | Action |
> |---|---|---|
> | Mid-session `store()` | Agent writes a cascading mutation | **Enqueue** affected dependents (5e-NEW-1) |
> | `end_session()` | Agent ends session | Apply judgment payload (5e-1..6) + **Drain** queue (5e-NEW-2) |
> | Factory job `QUEUED → PLANNING` | New factory job submitted | Drain queue (already shipped in 5c) |
>
> Without enqueue (5e-NEW-1), the drain triggers (factory + end_session)
> have nothing to do. They land together.

**Files:**
- Create: `factory/curator/end_session.py`
- Modify: `mcp-server/src/index.ts` — add new optional params to `end_session` tool schema
- Modify: `mcp-server/src/memory.ts` (or wherever `store` + `end_session` impls live) — add cascade detection + enqueue to `store()`; plumb `end_session()` enrichment + drain trigger
- Test: `factory/tests/test_curator_end_session.py`
- Test: `factory/tests/test_store_cascade_enqueue.py` (NEW — covers 5e-NEW-1)
- Postulate: `tests/postulates/test_p_end_session_isolation.py`
- Postulate: `tests/postulates/test_p_end_session_idempotent.py`

### Task 5e-NEW-1: `store()` cascade detection + enqueue

When `store()` is called, after the memory row is inserted/updated, detect
whether the mutation should cascade and enqueue affected dependents.

**Three cascade triggers** (matches the `edge_type` CHECK on `curator_re_eval_queue`):

1. **Writing a `supersedes` edge** — the new row has `supersedes=[old_id]`
   in its params; `old_id` is the cascade source. Walk
   `memory_dependencies` to find all rows where
   `to_memory_id=old_id AND edge_type='depends_on'`. Enqueue each as
   `(memory_id=dependent, cascade_source_id=old_id, edge_type='supersedes')`.
2. **Setting `archived_at`** — `store()` archived a memory. The archived
   memory_id is the cascade source. Walk dependents, enqueue with
   `edge_type='archived_at'`.
3. **Mutating `applies_when`** — `store()` updated a memory's
   `applies_when` JSONB. Walk dependents, enqueue with
   `edge_type='applies_when'`.

**Use `INSERT … ON CONFLICT (memory_id, cascade_source_id, edge_type) WHERE attempt_count < 3 DO NOTHING`** to dedup against the partial unique index from migration 017. PG 15+ supports WHERE-clause conflict targets.

**Implementation in `mcp-server/src/memory.ts`** (TypeScript). Pattern:

```typescript
// Inside the store handler, after the INSERT/UPDATE on devbrain.memory:
async function enqueueCascades(
  client: Pool,
  cascadeSourceId: string,
  edgeType: 'supersedes' | 'archived_at' | 'applies_when'
): Promise<void> {
  await client.query(
    `INSERT INTO devbrain.curator_re_eval_queue
       (memory_id, cascade_source_id, edge_type)
     SELECT from_memory_id, $1, $2
     FROM devbrain.memory_dependencies
     WHERE to_memory_id = $1 AND edge_type = 'depends_on'
     ON CONFLICT (memory_id, cascade_source_id, edge_type)
       WHERE attempt_count < 3 DO NOTHING`,
    [cascadeSourceId, edgeType]
  );
}

// Trigger points in store handler:
if (params.supersedes && params.supersedes.length > 0) {
  for (const oldId of params.supersedes) {
    await enqueueCascades(pool, oldId, 'supersedes');
  }
}
if (params.archived_at && !previousArchivedAt) {
  await enqueueCascades(pool, memoryId, 'archived_at');
}
if (params.applies_when && !deepEqual(params.applies_when, previousAppliesWhen)) {
  await enqueueCascades(pool, memoryId, 'applies_when');
}
```

**Test (`factory/tests/test_store_cascade_enqueue.py`)** — 3 integration
tests, one per cascade trigger. Each writes a memory with the trigger,
queries `curator_re_eval_queue`, asserts the expected dependent rows
appear with the correct `(memory_id, cascade_source_id, edge_type)`.
Use the `project_factory` + `memory_factory` fixtures from
`factory/tests/conftest.py` (added in 5c). Setup: insert source, insert
dependent, INSERT a `depends_on` edge, then exercise the store handler
(may need to drive via the MCP server end-to-end OR call the
TypeScript handler logic via a Python test wrapper — implementer's call).

**Coverage gate:** the `enqueueCascades` function and its three call
sites in the store handler must be exercised by these 3 tests.

### Task 5e-NEW-2: `end_session()` drain trigger

After `end_session_idempotent_handler` applies the structured judgment,
call `drain_one_batch(conn, batch_size=200)`. Larger batch than the
factory job startup default (50) because end_session is a natural
"catch up" moment.

**Add to `factory/curator/end_session.py`** at the end of
`end_session_idempotent_handler`, after the `INSERT INTO end_session_log`:

```python
# Drain everything the session enqueued (during work) plus what step 1 just added.
# Larger batch — end_session is a natural catch-up moment.
from curator.worker import drain_one_batch
try:
    drained = drain_one_batch(conn, batch_size=200)
    result["cascades_drained"] = drained
except Exception as exc:  # noqa: BLE001
    # Drain failure should NOT fail end_session — judgment already persisted.
    # Log and continue.
    import logging
    logging.getLogger(__name__).exception("end_session drain failed: %s", exc)
    result["cascades_drained"] = 0
    result["drain_error"] = str(exc)[:500]
```

The drain failure is non-fatal — judgment is the user-facing primary
work; drain is a "best-effort cleanup" that runs in the same call.
Failed drain rows stay in queue for the next end_session OR factory job.

**Test:** add to `factory/tests/test_curator_end_session.py`:
`test_end_session_drains_queue_after_applying_judgment` — set up cascade
queue rows pre-call, fire end_session, assert queue rows drained AND
strength values updated AND `cascades_drained` count in result.



### Task 5e-1: Implement `factory/curator/end_session.py`

```python
"""Handlers for the new structured-judgment params on end_session().

The calling agent (already in-context with the full session) volunteers
judgment via three optional params. This module persists those decisions
as side-effects.

NO LLM CALL HERE. The LLM call is the agent that called end_session()
itself — they're providing the judgment, we're persisting it.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class CascadeDecision(BaseModel):
    memory_id: UUID
    action: str  # promote | merge | contradict | refine | no_action
    rationale: str = ""


class NewEdge(BaseModel):
    from_memory_id: UUID
    to_memory_id: UUID
    edge_type: str  # depends_on | supersedes | contradicts | derived_from


class LessonCandidate(BaseModel):
    title: str
    content: str
    applies_when: dict = Field(default_factory=dict)
    compliance_profiles: list[str] = Field(default_factory=list)


def handle_cascade_decisions(
    conn: Any, session_project_id: UUID, decisions: list[CascadeDecision]
) -> None:
    """Apply per-memory actions volunteered by the calling agent.

    Validates every memory_id belongs to the session's project before
    applying ANY decisions (cross-project isolation — P_end_session_isolation).
    """
    if not decisions:
        return
    _assert_all_in_project(
        conn, session_project_id, [d.memory_id for d in decisions]
    )
    with conn.cursor() as cur:
        for d in decisions:
            if d.action == "promote":
                cur.execute(
                    "UPDATE devbrain.memory SET tier = 'lesson' "
                    "WHERE id = %s AND tier = 'memory'",
                    (d.memory_id,),
                )
            elif d.action == "contradict":
                # Mark for refinement — actual contradiction handling is Step 6.
                cur.execute(
                    "UPDATE devbrain.memory SET strength = strength * 0.5 "
                    "WHERE id = %s",
                    (d.memory_id,),
                )
            elif d.action == "refine":
                # Step 6 refinement agent picks this up; for now just enqueue.
                cur.execute(
                    "INSERT INTO devbrain.curator_re_eval_queue "
                    "(memory_id, cascade_source_id, edge_type) "
                    "VALUES (%s, %s, 'applies_when')",
                    (d.memory_id, d.memory_id),  # self-cascade signal
                )
            # merge / no_action: no-op for v3.0
    conn.commit()


def handle_new_relationships(
    conn: Any, session_project_id: UUID, edges: list[NewEdge]
) -> None:
    """Insert into memory_dependencies; ON CONFLICT DO NOTHING."""
    if not edges:
        return
    all_ids = [e.from_memory_id for e in edges] + [e.to_memory_id for e in edges]
    _assert_all_in_project(conn, session_project_id, all_ids)
    with conn.cursor() as cur:
        for e in edges:
            cur.execute(
                "INSERT INTO devbrain.memory_dependencies "
                "(from_memory_id, to_memory_id, edge_type, created_by) "
                "VALUES (%s, %s, %s, 'end_session') "
                "ON CONFLICT DO NOTHING",
                (e.from_memory_id, e.to_memory_id, e.edge_type),
            )
    conn.commit()


def handle_lesson_candidates(
    conn: Any, session_project_id: UUID, candidates: list[LessonCandidate]
) -> None:
    """Insert new tier='lesson' memory rows."""
    if not candidates:
        return
    with conn.cursor() as cur:
        for c in candidates:
            cur.execute(
                "INSERT INTO devbrain.memory "
                "(project_id, kind, title, content, tier, strength, applies_when) "
                "VALUES (%s, 'pattern', %s, %s, 'lesson', 1.0, %s::jsonb)",
                (session_project_id, c.title, c.content, _json(c.applies_when)),
            )
    conn.commit()


def _assert_all_in_project(conn, project_id, memory_ids):
    if not memory_ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory "
            "WHERE id = ANY(%s) AND project_id = %s",
            (list(memory_ids), project_id),
        )
        count = cur.fetchone()[0]
    if count != len(set(memory_ids)):
        raise ValueError(
            "end_session payload references memories outside the session's project"
        )


def _json(d: dict) -> str:
    import json as _json_mod
    return _json_mod.dumps(d)
```

### Task 5e-2: Postulate P_end_session_isolation

**File:** `tests/postulates/test_p_end_session_isolation.py`

```python
"""P_end_session_isolation — cross-project payload rejected wholesale.

POSTULATE
---------
If end_session() receives a cascade_decisions payload referencing a memory
from a different project than the session's, the entire payload is
rejected — NO partial application.
"""
from __future__ import annotations

import pytest

from curator.end_session import (
    CascadeDecision,
    handle_cascade_decisions,
)


def test_cross_project_decision_rejected_wholesale(
    conn, project_factory, memory_factory
):
    p1 = project_factory("iso1")
    p2 = project_factory("iso2")
    m_p1 = memory_factory(p1["id"], content="in p1")
    m_p2 = memory_factory(p2["id"], content="in p2")

    decisions = [
        CascadeDecision(memory_id=m_p1["id"], action="promote", rationale=""),
        CascadeDecision(memory_id=m_p2["id"], action="promote", rationale=""),
    ]

    with pytest.raises(ValueError, match="outside the session's project"):
        handle_cascade_decisions(conn, p1["id"], decisions)

    # Verify NO partial application — m_p1 stays at tier='memory', not promoted.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tier FROM devbrain.memory WHERE id = %s",
            (m_p1["id"],),
        )
        assert cur.fetchone()[0] == "memory"
```

### Task 5e-3: Postulate P_end_session_idempotent

**File:** `tests/postulates/test_p_end_session_idempotent.py`

```python
"""P_end_session_idempotent — same session calling end_session() twice = same state.

POSTULATE
---------
Two end_session() calls with the same session_id and identical payloads
produce identical observable state. The MCP server uses session_id as
idempotency key; the second call returns the first call's result without
re-applying side-effects.
"""
from __future__ import annotations

import pytest

# This postulate covers the MCP-server layer integration — not the Python
# helpers in factory/curator/end_session.py. The actual idempotency key
# enforcement lives in mcp-server/src/memory.ts (Step 5e-4).

# For a unit-style test of the same property, we exercise via a stub
# end_session_handler that records call hashes:

def test_idempotency_via_session_id_key(conn, project_factory, memory_factory):
    project = project_factory("idem")
    m = memory_factory(project["id"], content="x")

    from curator.end_session import end_session_idempotent_handler

    payload = {
        "session_id": "test-session-123",
        "summary": "first call",
        "cascade_decisions": [
            {"memory_id": str(m["id"]), "action": "promote", "rationale": ""}
        ],
        "new_relationships": [],
        "lesson_candidates": [],
    }

    r1 = end_session_idempotent_handler(conn, project["id"], payload)
    r2 = end_session_idempotent_handler(conn, project["id"], payload)
    assert r1 == r2

    # Promotion should only have happened once.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tier FROM devbrain.memory WHERE id = %s",
            (m["id"],),
        )
        assert cur.fetchone()[0] == "lesson"
```

### Task 5e-4: Implement `end_session_idempotent_handler` + idempotency table

Add a tiny `devbrain.end_session_log` table for idempotency keys:

**Add to `migrations/017_curator_queue_and_brief.sql`** (or a new 018 if 017 is already merged — the plan accommodates both):

```sql
CREATE TABLE IF NOT EXISTS devbrain.end_session_log (
    session_id   TEXT PRIMARY KEY,
    project_id   UUID NOT NULL REFERENCES devbrain.projects(id),
    payload_hash TEXT NOT NULL,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    result       JSONB
);
```

Add `end_session_idempotent_handler` to `factory/curator/end_session.py`:

```python
import hashlib
import json as _json

def end_session_idempotent_handler(conn, project_id, payload):
    """Apply end_session payload exactly once per (session_id, payload-hash)."""
    session_id = payload["session_id"]
    payload_hash = hashlib.sha256(_json.dumps(payload, sort_keys=True).encode()).hexdigest()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT result FROM devbrain.end_session_log "
            "WHERE session_id = %s AND payload_hash = %s",
            (session_id, payload_hash),
        )
        existing = cur.fetchone()
        if existing:
            return existing[0]

    # Apply once.
    handle_cascade_decisions(
        conn, project_id,
        [CascadeDecision(**d) for d in payload.get("cascade_decisions", [])],
    )
    handle_new_relationships(
        conn, project_id,
        [NewEdge(**e) for e in payload.get("new_relationships", [])],
    )
    handle_lesson_candidates(
        conn, project_id,
        [LessonCandidate(**c) for c in payload.get("lesson_candidates", [])],
    )

    result = {"status": "applied"}
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.end_session_log "
            "(session_id, project_id, payload_hash, result) "
            "VALUES (%s, %s, %s, %s::jsonb)",
            (session_id, project_id, payload_hash, _json.dumps(result)),
        )
    conn.commit()
    return result
```

### Task 5e-5: Extend MCP server `end_session` tool

**Modify:** `mcp-server/src/index.ts` (or wherever `end_session` is registered)

Add the three new optional params to the tool's input schema:

```typescript
end_session: {
  inputSchema: {
    type: "object",
    properties: {
      // ... existing: summary, files_changed, next_steps, decisions_made ...
      cascade_decisions: {
        type: "array",
        items: {
          type: "object",
          properties: {
            memory_id: { type: "string", format: "uuid" },
            action: { enum: ["promote","merge","contradict","refine","no_action"] },
            rationale: { type: "string" },
          },
          required: ["memory_id","action"],
        },
      },
      new_relationships: {
        type: "array",
        items: {
          type: "object",
          properties: {
            from_memory_id: { type: "string", format: "uuid" },
            to_memory_id: { type: "string", format: "uuid" },
            edge_type: { type: "string" },
          },
          required: ["from_memory_id","to_memory_id","edge_type"],
        },
      },
      lesson_candidates: {
        type: "array",
        items: {
          type: "object",
          properties: {
            title: { type: "string" },
            content: { type: "string" },
            applies_when: { type: "object" },
            compliance_profiles: { type: "array", items: { type: "string" } },
          },
          required: ["title","content"],
        },
      },
    },
  },
}
```

In the handler (`mcp-server/src/memory.ts`), after the existing summary persistence, call out to a Python helper or inline the SQL to invoke `end_session_idempotent_handler`. Practical path: shell out to a small Python entry point at `factory/curator/end_session_entry.py`:

```python
# factory/curator/end_session_entry.py
"""Entry point invoked from the MCP server's end_session handler."""
import json
import sys

from curator.end_session import end_session_idempotent_handler
from db import connect

def main():
    payload = json.load(sys.stdin)
    project_id = payload["project_id"]
    with connect() as conn:
        result = end_session_idempotent_handler(conn, project_id, payload)
    json.dump(result, sys.stdout)

if __name__ == "__main__":
    main()
```

Then in TypeScript:

```typescript
import { spawnSync } from "node:child_process";

// Inside end_session handler, after existing logic:
if (params.cascade_decisions || params.new_relationships || params.lesson_candidates) {
  const result = spawnSync(
    "python",
    ["-m", "curator.end_session_entry"],
    { input: JSON.stringify({ ...params, project_id }), encoding: "utf-8" }
  );
  if (result.status !== 0) {
    throw new Error(`end_session enrichment failed: ${result.stderr}`);
  }
}
```

Rebuild MCP server:

```bash
cd mcp-server && npm run build
```

### Task 5e-6: Run all postulates + commit + PR

```bash
cd factory && /Users/patrickkelly/devbrain/.venv/bin/python -m pytest tests/postulates/ -v
```

Expected: P1, P2, P3, P_cycle, P_archived_mid_cascade, P_stuck_surface_able, P_end_session_isolation, P_end_session_idempotent — 8 postulates green.

```bash
git add factory/curator/end_session.py factory/curator/end_session_entry.py mcp-server/src/index.ts mcp-server/src/memory.ts mcp-server/dist/ migrations/017_curator_queue_and_brief.sql tests/postulates/test_p_end_session_*.py factory/tests/test_curator_end_session.py
git commit -m "feat(curator): end_session enrichment + 2 postulates (Atlas Step 5e)"
git push
gh pr create --title "feat(curator): Atlas Step 5e — end_session enrichment" --body "MCP server end_session() accepts cascade_decisions, new_relationships, lesson_candidates from the calling agent (who has full session context — no separate LLM agent spawned). Cross-project payloads rejected wholesale. Idempotent on session_id + payload_hash. P_end_session_isolation and P_end_session_idempotent postulates pass."
```

---

## Phase 5f — DB-available CI workflow

**Files:**
- Modify: `.github/workflows/tests.yml` — add a `pytest-db` job with pgvector service container

### Task 5f-1: Write the workflow

Locate the existing `pytest` job in `.github/workflows/tests.yml`. Add a sibling job:

```yaml
  pytest-db:
    name: pytest (DB-available — postulates + integration)
    runs-on: ubuntu-latest
    timeout-minutes: 15
    services:
      postgres:
        image: pgvector/pgvector:pg16
        env:
          POSTGRES_USER: devbrain
          POSTGRES_PASSWORD: devbrain-ci
          POSTGRES_DB: devbrain
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U devbrain"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install deps
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt
          pip install pytest-cov
      - name: Apply migrations
        env:
          PGPASSWORD: devbrain-ci
        run: |
          for f in $(ls migrations/0*.sql | sort); do
            psql -h localhost -U devbrain -d devbrain -f "$f"
          done
      - name: Run postulates + integration
        env:
          DEVBRAIN_DB_HOST: localhost
          DEVBRAIN_DB_USER: devbrain
          DEVBRAIN_DB_PASSWORD: devbrain-ci
          DEVBRAIN_DB_NAME: devbrain
        run: |
          cd factory && python -m pytest tests/postulates/ tests/test_curator_*.py -v
```

### Task 5f-2: Verify CI runs green on the branch

```bash
git add .github/workflows/tests.yml
git commit -m "ci(tests): add DB-available job for postulates + curator integration (Atlas Step 5f)"
git push
gh run watch --branch feat/atlas-step-5-curator
```

Expected: both `pytest` (no-DB subset) and `pytest-db` (postulates + integration) pass.

### Task 5f-3: Final umbrella PR (or merge sub-PR 5f and call Step 5 done)

```bash
gh pr create --title "ci(tests): Atlas Step 5f — DB-available CI workflow" --body "Enables the postulate-running CI job that was deferred from Step 4. After this lands, all 8 postulates run on every push (P1, P2, P3 + 5 new from Step 5)."
```

---

## Verification matrix — when is Step 5 done?

| Gate | Source |
|---|---|
| P1 (supersession cascades) passes — was xfail | 5d |
| P2 (archived excluded) passes — was xfail | 5d |
| P3 (HIPAA cross-project isolation) still passes | regression |
| P_cycle passes | 5c |
| P_archived_mid_cascade passes | 5c |
| P_stuck_surface_able passes | 5c |
| P_end_session_isolation passes | 5e |
| P_end_session_idempotent passes | 5e |
| `factory/curator/strength.py` 100% coverage | 5b |
| `factory/curator/types.py` 100% coverage | 5a |
| `factory/curator/worker.py` ≥ 85% coverage | 5c |
| `factory/curator/brief.py` ≥ 85% coverage | 5d |
| DB-available CI workflow green on every push | 5f |
| Existing 180 no-DB tests still pass (no regression) | every PR |

After all six sub-PRs (5a → 5f) merge and the verification matrix is green, **Atlas Step 5 is done**. Step 6 (eval agents + lesson graduation) becomes unblocked.

## Implementation order recap

```
5a (PR) → 5b (PR) → 5c (PR) → 5d (PR — flips P1+P2 to passing) → 5e (PR) → 5f (PR — enables DB CI)
```

Each PR is independently reviewable. No big-bang merge.

---

## Open at implementation time (carried forward from design §8)

Resolve via PR review or while implementing:

- **Worker batch size** — start at 50 (in 5c), tune after observing real load
- **Worker poll interval** — start at 5s (in orchestrator integration, 5c-3), configurable via `config/devbrain.yaml`
- **Multi-hop penalty threshold** — currently 0.05 (in 5c-2), revisit after first real cascade
- **Lesson `top_n` in brief** — currently 20 (in 5d-2), revisit when brief rendering is observed
- **Naive applies_when matcher** — substring match (in 5d-2 `_load_decisions_matching`); Phase 3.x replaces
- **Stuck-queue alert threshold** — manual triage via CLI for v3.0; notification in Phase 3.x
