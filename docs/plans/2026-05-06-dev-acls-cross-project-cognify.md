# Dev ACLs + Cross-Project Cognify Edges Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add per-project access controls to `devbrain.devs` (Commit 1) and a `cross_project` flag to `cognify_edges` that surfaces canonical 'devbrain' rule memories as contradiction candidates (Commit 2).

**Architecture:** Commit 1 adds an `allowed_projects TEXT[]` column to the devs table (NULL = all projects, preserves backward compat), wires ACL enforcement into `FactoryDB.create_job`, and updates the `setup add-dev` wizard to collect the restriction. Commit 2 adds a `cross_project: bool = False` parameter to `EdgesPass.run` and `_detect_contradicts` that, when True, resolves the canonical 'devbrain' project_id and loads its memories alongside the current project's memories as contradiction candidates — strictly one-way (canonical → any project, never project A → project B).

**Tech Stack:** Python 3.11+, psycopg2, Click, pytest, Postgres 17 (TEXT[] native), `devbrain.devs`, `devbrain.projects`, `devbrain.memory`, `devbrain.memory_dependencies`

---

## Commit 1: `feat(devs): per-project access controls`

### Task 1-A: Write and run migration 028

**Files:**
- Create: `migrations/028_dev_allowed_projects.sql`

**Step 1: Write the migration**

```sql
-- ─────────────────────────────────────────────────────────────────────────────
-- 028: Add allowed_projects to devbrain.devs
-- ─────────────────────────────────────────────────────────────────────────────
--
-- NULL  = dev can submit to any project (default, existing-dev behavior preserved).
-- '{}'  = dev is locked out of all projects.
-- '{slug1,slug2}' = dev can only submit to those slugs.
--
-- Uses project SLUGS (not UUIDs) for portability across DB instances.

ALTER TABLE devbrain.devs
    ADD COLUMN IF NOT EXISTS allowed_projects TEXT[] DEFAULT NULL;

COMMENT ON COLUMN devbrain.devs.allowed_projects IS
    'NULL = all projects allowed. Empty array = no projects. '
    'Otherwise: list of project slugs this dev may submit jobs to. '
    'Added in migration 028.';
```

**Step 2: Apply migration**

```bash
PW=$(awk -F': ' '/^[[:space:]]*password:/ {print $2; exit}' /Users/patrickkelly/devbrain/.worktrees/dev-acls/config/devbrain.yaml | tr -d '"' | head -1)
PGPASSWORD=$PW psql -h 127.0.0.1 -p 5433 -U devbrain -d devbrain \
  -f /Users/patrickkelly/devbrain/.worktrees/dev-acls/migrations/028_dev_allowed_projects.sql
```

Expected: `ALTER TABLE` / `COMMENT`

**Step 3: Verify the column exists**

```bash
PW=$(awk -F': ' '/^[[:space:]]*password:/ {print $2; exit}' /Users/patrickkelly/devbrain/.worktrees/dev-acls/config/devbrain.yaml | tr -d '"' | head -1)
PGPASSWORD=$PW psql -h 127.0.0.1 -p 5433 -U devbrain -d devbrain \
  -c "\d devbrain.devs" | grep allowed_projects
```

Expected: line with `allowed_projects | text[]`

---

### Task 1-B: Write failing unit tests for ACL enforcement

**Files:**
- Create: `factory/tests/test_dev_acls.py`

**Step 1: Write the failing tests**

```python
"""Tests for per-project dev ACL enforcement in FactoryDB.create_job.

Migration 028 adds devbrain.devs.allowed_projects TEXT[]:
  NULL  → all projects allowed (default; preserves existing behavior)
  []    → no projects allowed
  ['slug1'] → only that slug allowed
"""
from __future__ import annotations

import pytest
from state_machine import FactoryDB


@pytest.fixture
def db(database_url):
    return FactoryDB(database_url)


@pytest.fixture(autouse=True)
def cleanup(db):
    """Remove test devs before and after each test."""
    def _purge():
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM devbrain.devs WHERE dev_id LIKE 'test_acl_%%'"
            )
            conn.commit()
    _purge()
    yield
    _purge()


@pytest.mark.db
def test_null_allowed_projects_permits_any_project(db, project_factory):
    """Dev with allowed_projects=NULL can submit to any project."""
    project = project_factory("acl_open")
    dev_id = "test_acl_null"
    db.register_dev(dev_id, full_name="Null ACL Dev")
    # allowed_projects defaults to NULL; no explicit set needed

    # Should not raise
    job_id = db.create_job(
        project_slug=project["slug"],
        title="test job",
        spec="test",
        submitted_by=dev_id,
    )
    assert job_id


@pytest.mark.db
def test_empty_allowed_projects_blocks_all(db, project_factory):
    """Dev with allowed_projects=[] cannot submit to any project."""
    project = project_factory("acl_empty")
    dev_id = "test_acl_empty"
    db.register_dev(dev_id, full_name="Locked Out Dev")
    db.set_dev_allowed_projects(dev_id, [])

    with pytest.raises(PermissionError, match="not permitted"):
        db.create_job(
            project_slug=project["slug"],
            title="blocked job",
            spec="test",
            submitted_by=dev_id,
        )


@pytest.mark.db
def test_specific_slug_allows_matching_project(db, project_factory):
    """Dev restricted to ['brightbot'] can submit to 'brightbot' but not others."""
    allowed_project = project_factory("brightbot")
    other_project = project_factory("lht_vps")
    dev_id = "test_acl_specific"
    db.register_dev(dev_id, full_name="Restricted Dev")
    db.set_dev_allowed_projects(dev_id, [allowed_project["slug"]])

    # Allowed project: should succeed
    job_id = db.create_job(
        project_slug=allowed_project["slug"],
        title="allowed job",
        spec="test",
        submitted_by=dev_id,
    )
    assert job_id

    # Other project: should raise
    with pytest.raises(PermissionError, match="not permitted"):
        db.create_job(
            project_slug=other_project["slug"],
            title="blocked job",
            spec="test",
            submitted_by=dev_id,
        )


@pytest.mark.db
def test_unknown_dev_submitted_by_skips_acl_check(db, project_factory):
    """If submitted_by is None or not a registered dev, the ACL check is skipped."""
    project = project_factory("acl_anon")

    # No submitted_by — should not raise
    job_id = db.create_job(
        project_slug=project["slug"],
        title="anon job",
        spec="test",
        submitted_by=None,
    )
    assert job_id


@pytest.mark.db
def test_set_dev_allowed_projects_persists(db):
    """set_dev_allowed_projects stores the value and get_dev returns it."""
    dev_id = "test_acl_persist"
    db.register_dev(dev_id, full_name="Persist Dev")
    db.set_dev_allowed_projects(dev_id, ["brightbot", "lht-vps"])

    dev = db.get_dev(dev_id)
    assert dev["allowed_projects"] == ["brightbot", "lht-vps"]


@pytest.mark.db
def test_set_dev_allowed_projects_null_restores_all_access(db, project_factory):
    """Setting allowed_projects back to None restores unrestricted access."""
    project = project_factory("acl_restore")
    dev_id = "test_acl_restore"
    db.register_dev(dev_id, full_name="Restore Dev")
    db.set_dev_allowed_projects(dev_id, [])  # lock out
    db.set_dev_allowed_projects(dev_id, None)  # restore

    # Should not raise
    job_id = db.create_job(
        project_slug=project["slug"],
        title="restored job",
        spec="test",
        submitted_by=dev_id,
    )
    assert job_id
```

**Step 2: Run tests to verify they fail**

```bash
cd /Users/patrickkelly/devbrain/.worktrees/dev-acls/factory
DEVBRAIN_DB_PASSWORD=$(awk -F': ' '/^[[:space:]]*password:/ {print $2; exit}' ../config/devbrain.yaml | tr -d '"') \
  pytest tests/test_dev_acls.py -v -m db 2>&1 | head -50
```

Expected: FAILED (AttributeError: 'FactoryDB' object has no attribute 'set_dev_allowed_projects')

---

### Task 1-C: Implement `set_dev_allowed_projects` and ACL check in `state_machine.py`

**Files:**
- Modify: `factory/state_machine.py`

**Changes needed in two places:**

**Change 1 — Add `set_dev_allowed_projects` method** (add after `remove_dev_channel`, before `record_notification`):

```python
def set_dev_allowed_projects(
    self, dev_id: str, allowed_projects: list[str] | None
) -> None:
    """Set the projects this dev may submit jobs to.

    allowed_projects=None  → unrestricted (all projects).
    allowed_projects=[]    → locked out of all projects.
    allowed_projects=[...] → restricted to named slugs.
    Uses project SLUGS for portability across DB instances.
    """
    dev = self.get_dev(dev_id)
    if not dev:
        raise ValueError(f"Dev '{dev_id}' not found")
    with self._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.devs SET allowed_projects = %s, updated_at = now() "
            "WHERE dev_id = %s",
            (allowed_projects, dev_id),
        )
        conn.commit()
```

**Change 2 — Add ACL check in `create_job`** (add after project lookup, before INSERT):

```python
# ACL: if submitted_by names a registered dev with a non-NULL
# allowed_projects list, verify the target project is in the list.
if submitted_by:
    cur.execute(
        "SELECT allowed_projects FROM devbrain.devs WHERE dev_id = %s",
        (submitted_by,),
    )
    dev_row = cur.fetchone()
    if dev_row is not None:
        allowed = dev_row[0]  # TEXT[] or None
        if allowed is not None and project_slug not in allowed:
            raise PermissionError(
                f"Dev '{submitted_by}' is not permitted to submit jobs "
                f"to project '{project_slug}'."
            )
```

**Change 3 — Update `get_dev` to return `allowed_projects`** (add the column to the SELECT and return dict):

```python
# In get_dev: change SELECT to include allowed_projects
"SELECT id, dev_id, full_name, channels, event_subscriptions, "
"       created_at, updated_at, allowed_projects "
# And in the return dict:
"allowed_projects": row[7],  # None or list of slugs
```

**Change 4 — Update `list_devs` similarly** (add column + return field).

**Step 2: Run tests to verify they pass**

```bash
cd /Users/patrickkelly/devbrain/.worktrees/dev-acls/factory
DEVBRAIN_DB_PASSWORD=$(awk -F': ' '/^[[:space:]]*password:/ {print $2; exit}' ../config/devbrain.yaml | tr -d '"') \
  pytest tests/test_dev_acls.py -v -m db 2>&1
```

Expected: all PASS

**Step 3: Run the full test suite to verify no regressions**

```bash
cd /Users/patrickkelly/devbrain/.worktrees/dev-acls/factory
DEVBRAIN_DB_PASSWORD=$(awk -F': ' '/^[[:space:]]*password:/ {print $2; exit}' ../config/devbrain.yaml | tr -d '"') \
  pytest tests/ -v -m db -x --timeout=60 2>&1 | tail -30
```

Expected: all existing tests pass

---

### Task 1-D: Update `setup_add_dev` wizard to collect `allowed_projects`

**Files:**
- Modify: `factory/setup.py` (function `setup_add_dev`, starting around line 1957)

**Step 1: Add the prompt** (add after the `notes` prompt, before the "Show summary, confirm" block):

```python
raw_projects = _prompt(
    "Restrict this dev to specific projects? "
    "Comma-separated slugs, or empty for all projects",
    default="",
).strip()
allowed_projects: list[str] | None = None
if raw_projects:
    allowed_projects = [s.strip() for s in raw_projects.split(",") if s.strip()]
```

**Step 2: Show it in the summary** (add after the `notes` echo block):

```python
if allowed_projects is not None:
    click.echo(f"   allowed_projects: {', '.join(allowed_projects) or '(none — locked out)'}")
else:
    click.echo(f"   allowed_projects: (all projects)")
```

**Step 3: Persist it** (add after `db.register_dev(...)` call):

```python
if allowed_projects is not None:
    db.set_dev_allowed_projects(dev_id, allowed_projects)
```

**Step 4: Run the existing setup multi-dev tests to check for regressions**

```bash
cd /Users/patrickkelly/devbrain/.worktrees/dev-acls/factory
DEVBRAIN_DB_PASSWORD=$(awk -F': ' '/^[[:space:]]*password:/ {print $2; exit}' ../config/devbrain.yaml | tr -d '"') \
  pytest tests/test_setup_multi_dev.py tests/test_devs_notifications_crud.py -v -m db 2>&1 | tail -20
```

Expected: all PASS

---

### Task 1-E: Write postulate `test_p_dev_project_isolation.py`

**Files:**
- Create: `tests/postulates/test_p_dev_project_isolation.py`

**Step 1: Write the postulate**

```python
"""P_dev_project_isolation — dev with allowed_projects cannot submit to other projects.

POSTULATE
---------
A dev whose allowed_projects=['brightbot'] is blocked from submitting
a job to any other project slug. A dev with allowed_projects=NULL
(default) is never blocked.

STATUS
------
Active. Migration 028. Enforced in FactoryDB.create_job.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "factory"))

from state_machine import FactoryDB


@pytest.fixture
def db(database_url):
    return FactoryDB(database_url)


@pytest.fixture(autouse=True)
def cleanup(db):
    def _purge():
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM devbrain.devs WHERE dev_id LIKE 'postulate_devacl_%%'"
            )
            conn.commit()
    _purge()
    yield
    _purge()


@pytest.mark.db
def test_dev_restricted_to_brightbot_cannot_submit_to_lht_vps(
    db, project_factory
):
    """Core isolation check: allowed_projects=['brightbot'] blocks lht-vps."""
    brightbot = project_factory("brightbot")
    lht_vps = project_factory("lht_vps")
    dev_id = "postulate_devacl_isolated"
    db.register_dev(dev_id, full_name="Isolated Dev")
    db.set_dev_allowed_projects(dev_id, [brightbot["slug"]])

    # Can submit to brightbot
    job_id = db.create_job(
        project_slug=brightbot["slug"],
        title="allowed job",
        spec="test",
        submitted_by=dev_id,
    )
    assert job_id

    # Cannot submit to lht-vps
    with pytest.raises(PermissionError):
        db.create_job(
            project_slug=lht_vps["slug"],
            title="blocked job",
            spec="test",
            submitted_by=dev_id,
        )


@pytest.mark.db
def test_dev_with_null_allowed_projects_is_never_blocked(db, project_factory):
    """NULL allowed_projects = unrestricted; no PermissionError on any project."""
    project_a = project_factory("acl_p_a")
    project_b = project_factory("acl_p_b")
    dev_id = "postulate_devacl_open"
    db.register_dev(dev_id, full_name="Open Dev")
    # allowed_projects is NULL by default

    for project in [project_a, project_b]:
        job_id = db.create_job(
            project_slug=project["slug"],
            title="open job",
            spec="test",
            submitted_by=dev_id,
        )
        assert job_id
```

**Step 2: Run the postulate**

```bash
cd /Users/patrickkelly/devbrain/.worktrees/dev-acls
DEVBRAIN_DB_PASSWORD=$(awk -F': ' '/^[[:space:]]*password:/ {print $2; exit}' config/devbrain.yaml | tr -d '"') \
  pytest tests/postulates/test_p_dev_project_isolation.py -v -m db 2>&1
```

Expected: all PASS

---

### Task 1-F: Commit 1

**Step 1: Stage and commit**

```bash
cd /Users/patrickkelly/devbrain/.worktrees/dev-acls
git add \
  migrations/028_dev_allowed_projects.sql \
  factory/state_machine.py \
  factory/setup.py \
  factory/tests/test_dev_acls.py \
  tests/postulates/test_p_dev_project_isolation.py
git commit -m "$(cat <<'EOF'
feat(devs): per-project access controls

Migration 028 adds allowed_projects TEXT[] to devbrain.devs.
NULL = all projects (default, no behavior change for existing devs).
[] = locked out. ['slug'] = restricted to named slugs.

FactoryDB.create_job enforces the ACL when submitted_by names a
registered dev with a non-NULL allowed_projects list.
set_dev_allowed_projects() added to FactoryDB for programmatic updates.
setup add-dev wizard prompts for the restriction at onboarding time.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Commit 2: `feat(cognify): cross_project flag on cognify_edges for canonical rule sweeps`

### Task 2-A: Write failing tests for cross_project cognify_edges

**Files:**
- Create: `factory/tests/test_cognify_edges_cross_project.py`

**Step 1: Write the failing tests**

```python
"""Tests for cross_project flag on cognify_edges EdgesPass.

When cross_project=True, _detect_contradicts uses memories from both
the target project and the canonical 'devbrain' project as candidates.
When cross_project=False (default), only target-project memories are used.

The canonical project restriction is strict: memories from project A
never surface as candidates when processing project B (unless A IS
the canonical 'devbrain' project). This matches the Phase 5 walker
design where cross_project surfaces canonical rules, not lateral projects.
"""
from __future__ import annotations

import uuid
import pytest

from cognify.edges import (
    EdgesPass,
    _detect_contradicts,
    _load_memories,
)


def _insert_memory(conn, project_id, title, content, kind="decision"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory "
            "(project_id, kind, title, content) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (project_id, kind, title, content),
        )
        mid = cur.fetchone()[0]
    conn.commit()
    return mid


def _get_or_create_canonical_project(conn):
    """Return the id of the canonical 'devbrain' project, creating it if absent."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.projects WHERE slug = 'devbrain'"
        )
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute(
            "INSERT INTO devbrain.projects (slug, name) "
            "VALUES ('devbrain', 'DevBrain Canonical') RETURNING id"
        )
        pid = cur.fetchone()[0]
    conn.commit()
    return pid


@pytest.mark.db
def test_cross_project_false_only_loads_own_project_memories(
    conn, project_factory
):
    """cross_project=False: _load_memories returns only target project rows."""
    project = project_factory("edges_cp_false")
    canonical_id = _get_or_create_canonical_project(conn)

    _insert_memory(conn, project["id"], "TargetMem", "target content")
    _insert_memory(conn, canonical_id, "CanonicalMem", "canonical content")

    memories = _load_memories(conn, project["id"])
    ids = {str(m["id"]) for m in memories}

    # Only target project memories
    titles = {m["title"] for m in memories}
    assert "TargetMem" in titles
    assert "CanonicalMem" not in titles


@pytest.mark.db
def test_cross_project_true_includes_canonical_memories(
    conn, project_factory
):
    """cross_project=True: _load_memories_for_edges includes canonical project rows."""
    project = project_factory("edges_cp_true")
    canonical_id = _get_or_create_canonical_project(conn)

    _insert_memory(conn, project["id"], "TargetMem2", "target content 2")
    _insert_memory(conn, canonical_id, "CanonicalMem2", "canonical rule content")

    # Import the cross_project-aware loader (to be implemented)
    from cognify.edges import _load_memories_cross_project

    memories = _load_memories_cross_project(conn, project["id"])
    titles = {m["title"] for m in memories}

    assert "TargetMem2" in titles
    assert "CanonicalMem2" in titles


@pytest.mark.db
def test_cross_project_true_does_not_include_other_non_canonical_projects(
    conn, project_factory
):
    """cross_project=True only adds the canonical devbrain project, not random others."""
    project = project_factory("edges_cp_scope")
    other_project = project_factory("edges_cp_other")
    canonical_id = _get_or_create_canonical_project(conn)

    _insert_memory(conn, project["id"], "OwnMem", "own content")
    _insert_memory(conn, other_project["id"], "OtherMem", "other project content")
    _insert_memory(conn, canonical_id, "CanonMem", "canonical content")

    from cognify.edges import _load_memories_cross_project

    memories = _load_memories_cross_project(conn, project["id"])
    titles = {m["title"] for m in memories}

    assert "OwnMem" in titles
    assert "CanonMem" in titles
    assert "OtherMem" not in titles  # non-canonical projects NOT included


@pytest.mark.db
def test_edges_pass_run_cross_project_flag_accepted(conn, project_factory):
    """EdgesPass.run accepts cross_project kwarg without raising."""
    project = project_factory("edges_cp_run")
    pass_ = EdgesPass()
    # Should not raise — even with no memories to process
    result = pass_.run(conn, project["id"], dry_run=True, cross_project=True)
    assert result is not None


@pytest.mark.db
def test_edges_pass_run_cross_project_default_false(conn, project_factory):
    """EdgesPass.run default cross_project=False behaves identically to before."""
    project = project_factory("edges_cp_default")
    pass_ = EdgesPass()
    result_default = pass_.run(conn, project["id"], dry_run=True)
    result_explicit = pass_.run(conn, project["id"], dry_run=True, cross_project=False)
    # Both should succeed with same row counts (0 memories = 0 candidates)
    assert result_default.rows_processed == result_explicit.rows_processed
```

**Step 2: Run tests to verify they fail**

```bash
cd /Users/patrickkelly/devbrain/.worktrees/dev-acls/factory
DEVBRAIN_DB_PASSWORD=$(awk -F': ' '/^[[:space:]]*password:/ {print $2; exit}' ../config/devbrain.yaml | tr -d '"') \
  pytest tests/test_cognify_edges_cross_project.py -v -m db 2>&1 | head -40
```

Expected: FAILED (ImportError: cannot import name '_load_memories_cross_project' or TypeError on cross_project kwarg)

---

### Task 2-B: Implement `cross_project` in `factory/cognify/edges.py`

**Files:**
- Modify: `factory/cognify/edges.py`

**Step 1: Add `_load_memories_cross_project` helper** (add after `_load_memories`):

```python
def _load_memories_cross_project(conn: Any, project_id: Any) -> list[dict]:
    """Load non-archived memory rows for project_id PLUS the canonical 'devbrain' project.

    The canonical project is identified by slug='devbrain'. If the caller's
    project IS 'devbrain', this is identical to _load_memories (no double-load).
    Non-canonical foreign projects are never included — this is a strict
    one-way bridge from canonical → any project, matching Phase 5 semantics.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.projects WHERE slug = 'devbrain'"
        )
        canonical_row = cur.fetchone()

    if canonical_row is None or canonical_row[0] == project_id:
        # No canonical project found, or we ARE the canonical project —
        # fall back to single-project load.
        return _load_memories(conn, project_id)

    canonical_id = canonical_row[0]
    project_ids = [project_id, canonical_id]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, kind, title, content "
            "FROM devbrain.memory "
            "WHERE project_id = ANY(%s) AND archived_at IS NULL "
            "ORDER BY created_at",
            (project_ids,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
```

**Step 2: Update `_run_edges` and `EdgesPass.run` to accept and thread `cross_project`:**

```python
# In EdgesPass.run — add cross_project kwarg:
def run(self, conn: Any, project_id: Any, *, dry_run: bool = False, cross_project: bool = False) -> PassResult:
    if project_id is None:
        raise ValueError(
            "cognify_edges requires a project_id (LLM pass; project-scoped)"
        )
    cites_new, contradicts_new, llm_calls = _run_edges(
        conn, project_id, dry_run=dry_run, cross_project=cross_project
    )
    return PassResult(
        rows_processed=cites_new + contradicts_new,
        llm_calls=llm_calls,
        metadata={
            "pass": "edges",
            "cites_edges_created": cites_new,
            "contradicts_edges_created": contradicts_new,
            "cross_project": cross_project,
        },
    )

# In _run_edges — add cross_project kwarg:
def _run_edges(
    conn: Any, project_id: Any, *, dry_run: bool = False, cross_project: bool = False
) -> tuple[int, int, int]:
    cites_new = _detect_cites(conn, project_id, dry_run=dry_run)
    contradicts_new, llm_calls = _detect_contradicts(
        conn, project_id, dry_run=dry_run, cross_project=cross_project
    )
    return cites_new, contradicts_new, llm_calls
```

**Step 3: Update `_detect_contradicts` to use `_load_memories_cross_project` when requested:**

```python
def _detect_contradicts(
    conn: Any, project_id: Any, *, dry_run: bool = False, cross_project: bool = False
) -> tuple[int, int]:
    # Choose loader based on flag
    if cross_project:
        memories = _load_memories_cross_project(conn, project_id)
    else:
        memories = _load_memories(conn, project_id)
    # ... rest unchanged ...
```

Note: `_detect_cites` does NOT change — cites detection is text-matching within the project's own memories only. Cross-project cites would produce spurious edges between unrelated projects.

**Step 4: Run tests to verify they pass**

```bash
cd /Users/patrickkelly/devbrain/.worktrees/dev-acls/factory
DEVBRAIN_DB_PASSWORD=$(awk -F': ' '/^[[:space:]]*password:/ {print $2; exit}' ../config/devbrain.yaml | tr -d '"') \
  pytest tests/test_cognify_edges_cross_project.py tests/test_cognify_edges.py -v -m db 2>&1
```

Expected: all PASS

---

### Task 2-C: Update CLI `cognify` command to accept `--cross-project`

**Files:**
- Modify: `factory/cli.py` (function `cognify_command`, around line 2816)

**Step 1: Add the option** (add after `--dry-run` option):

```python
@click.option(
    "--cross-project", "cross_project", is_flag=True, default=False,
    help="edges pass only: include canonical 'devbrain' project memories as contradiction candidates.",
)
```

**Step 2: Update the function signature and pass-through:**

```python
def cognify_command(pass_name, run_all, project_slug, dry_run, as_json, cross_project):
```

And in the single-pass branch, thread cross_project through:

```python
result = _run_pass(conn, pass_name, project_id, dry_run=dry_run, cross_project=cross_project)
```

Note: `run_pass` in `orchestrator.py` routes kwargs to the pass instance via `instance.run(conn, project_id, dry_run=dry_run, **kwargs)` — but currently it doesn't forward extra kwargs. We need to update `orchestrator.run_pass` to forward `**kwargs`.

**Step 3: Update `orchestrator.run_pass` to forward kwargs:**

In `factory/cognify/orchestrator.py`, change the `run_pass` call:

```python
# Before:
result = instance.run(conn, project_id, dry_run=dry_run)

# After:
result = instance.run(conn, project_id, dry_run=dry_run, **kwargs)
```

And update the signature:

```python
def run_pass(
    conn: Any,
    pass_name: str,
    project_id: Any = None,
    *,
    dry_run: bool = False,
    **kwargs,
) -> PassResult:
```

**Step 4: Run cognify tests to check for regressions**

```bash
cd /Users/patrickkelly/devbrain/.worktrees/dev-acls/factory
DEVBRAIN_DB_PASSWORD=$(awk -F': ' '/^[[:space:]]*password:/ {print $2; exit}' ../config/devbrain.yaml | tr -d '"') \
  pytest tests/test_cognify_edges.py tests/test_cognify_edges_cross_project.py \
         tests/test_cognify_extract.py tests/test_cognify_decay.py \
         tests/test_cognify_gc.py tests/test_cognify_strengthen.py \
         -v -m db 2>&1 | tail -30
```

Expected: all PASS

---

### Task 2-D: Run all tests end-to-end

**Step 1: Run full factory test suite**

```bash
cd /Users/patrickkelly/devbrain/.worktrees/dev-acls/factory
DEVBRAIN_DB_PASSWORD=$(awk -F': ' '/^[[:space:]]*password:/ {print $2; exit}' ../config/devbrain.yaml | tr -d '"') \
  pytest tests/ -v -m db -x --timeout=60 2>&1 | tail -40
```

Expected: all PASS (no regressions)

**Step 2: Run postulates**

```bash
cd /Users/patrickkelly/devbrain/.worktrees/dev-acls
DEVBRAIN_DB_PASSWORD=$(awk -F': ' '/^[[:space:]]*password:/ {print $2; exit}' config/devbrain.yaml | tr -d '"') \
  pytest tests/postulates/ -v -m db --timeout=60 2>&1 | tail -30
```

Expected: all PASS

---

### Task 2-E: Commit 2

**Step 1: Stage and commit**

```bash
cd /Users/patrickkelly/devbrain/.worktrees/dev-acls
git add \
  factory/cognify/edges.py \
  factory/cognify/orchestrator.py \
  factory/cli.py \
  factory/tests/test_cognify_edges_cross_project.py
git commit -m "$(cat <<'EOF'
feat(cognify): cross_project flag on cognify_edges for canonical rule sweeps

EdgesPass.run accepts cross_project=True to surface memories from the
canonical 'devbrain' project alongside the target project's own memories
when building contradiction candidate pairs.

Strict scoping: only the canonical 'devbrain' project is bridged —
never lateral project-to-project. Matches Phase 5 walker design.
cites detection is unchanged (text-match stays single-project).
CLI: devbrain cognify --pass=edges [--cross-project]
orchestrator.run_pass now forwards **kwargs to pass instances.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Push and open PR

**Step 1: Switch GitHub auth and push**

```bash
gh auth switch -u nooma-stack
git push -u origin feat/dev-acls-cross-project
```

**Step 2: Open the PR**

```bash
gh pr create \
  --title "feat: per-project dev ACLs + cross_project flag for cognify_edges" \
  --body "$(cat <<'EOF'
## Summary

- **Migration 028**: adds `allowed_projects TEXT[]` to `devbrain.devs`. NULL = all projects (existing devs unaffected). `[]` = locked out. `['slug']` = restricted to named slugs.
- **FactoryDB.create_job**: enforces ACL when `submitted_by` is a registered dev with a non-NULL `allowed_projects`. Unknown/anonymous submitters skip the check.
- **setup add-dev wizard**: prompts for project restriction at onboarding time.
- **cognify_edges cross_project**: `EdgesPass.run(cross_project=True)` loads canonical 'devbrain' project memories alongside target project when building contradiction candidate pairs. Strict one-way bridge — no lateral project-to-project surfacing. `devbrain cognify --pass=edges --cross-project` exposes this from the CLI.

## Test plan

- [ ] `factory/tests/test_dev_acls.py` — 6 tests covering NULL/empty/specific ACL cases and `set_dev_allowed_projects` round-trip
- [ ] `tests/postulates/test_p_dev_project_isolation.py` — isolation postulate for brightbot→lht-vps block
- [ ] `factory/tests/test_cognify_edges_cross_project.py` — 5 tests covering loader isolation, canonical inclusion, non-canonical exclusion, and kwarg plumbing
- [ ] Full factory test suite passes with no regressions
- [ ] Full postulate suite passes with no regressions

🤖 Generated with [Claude Code](https://claude.ai/claude-code)
EOF
)"
```

**Step 3: Verify CI**

```bash
gh pr checks
```

Wait for CI to pass. If any test fails, read the failure, fix, push another commit.
