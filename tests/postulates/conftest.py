"""Shared fixtures for the AGM-style postulate tests.

These tests run against a real Postgres (devbrain-db on 127.0.0.1:5433
by default). They are deliberately excluded from the no-DB CI subset
in .github/workflows/test.yml — see tests/postulates/README.md for the
local invocation. A DB-available CI workflow is tracked as follow-up
work in that same comment.
"""
from __future__ import annotations

import os
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")


# Every postulate-test row is tagged with this prefix so the cleanup
# fixture can wipe its own droppings without touching real data.
TEST_TAG = "postulate_test_"


def _database_url() -> str:
    explicit = os.getenv("DEVBRAIN_TEST_DATABASE_URL")
    if explicit:
        return explicit
    user = os.getenv("DEVBRAIN_DB_USER", "devbrain")
    password = os.getenv("DEVBRAIN_DB_PASSWORD")
    host = os.getenv("DEVBRAIN_DB_HOST", "127.0.0.1")
    port = os.getenv("DEVBRAIN_DB_HOST_PORT", "5433")
    name = os.getenv("DEVBRAIN_DB_NAME", "devbrain")
    if not password:
        pytest.skip(
            "DEVBRAIN_DB_PASSWORD (or DEVBRAIN_TEST_DATABASE_URL) not set; "
            "postulate tests require a real Postgres."
        )
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


@pytest.fixture(scope="session")
def database_url() -> str:
    return _database_url()


@pytest.fixture
def conn(database_url):
    """Per-test connection with autocommit OFF so cleanup is total.

    Registers psycopg2's UUID adapter on the module so id columns flow
    in/out as `uuid.UUID` consistently — without this, the adapter is
    only registered when state_machine.py happens to be imported.
    """
    import psycopg2.extras
    psycopg2.extras.register_uuid()
    c = psycopg2.connect(database_url)
    try:
        # Annotate this session as a postulate test so audit ledger rows
        # are easy to spot. Read by the trigger via current_setting().
        with c.cursor() as cur:
            cur.execute("SET devbrain.actor = 'postulate-test'")
        c.commit()
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture
def project_factory(conn):
    """Create disposable projects with a unique slug. Cleaned up at teardown.

    Optional kwarg `compliance_profiles_enabled` (Atlas Step 7a) seeds
    devbrain.projects.compliance_profiles_enabled at insert. Only set
    when non-None so the fixture stays usable on installs that haven't
    applied migration 022 yet.
    """
    created: list[str] = []

    def make(
        slug_hint: str = "p",
        *,
        compliance_profiles_enabled: list[str] | None = None,
    ) -> dict:
        slug = f"{TEST_TAG}{slug_hint}_{uuid.uuid4().hex[:8]}"
        cols = ["slug", "name"]
        vals: list = [slug, f"Postulate Test {slug_hint}"]
        if compliance_profiles_enabled is not None:
            cols.append("compliance_profiles_enabled")
            vals.append(compliance_profiles_enabled)
        placeholders = ", ".join(["%s"] * len(cols))
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO devbrain.projects ({', '.join(cols)}) "
                f"VALUES ({placeholders}) RETURNING id, slug",
                vals,
            )
            row = cur.fetchone()
        conn.commit()
        created.append(row[0])
        return {"id": row[0], "slug": row[1]}

    yield make

    # The test itself may have left the connection in an aborted-
    # transaction state (e.g. an xfail test that probed a missing
    # table). Roll back before cleanup so the DELETE statements run.
    conn.rollback()

    # Order: queue rows + ledger rows first (FK to memory), then deps,
    # then end_session_log (FK to project), then memory, then project.
    # Phase 5e adds end_session_log (FK project_id) and reuses the
    # curator_re_eval_queue from Phase 5a (FK memory_id ON DELETE CASCADE
    # but cascade_source_id plain REFERENCES, so explicit DELETE first).
    with conn.cursor() as cur:
        for pid in created:
            cur.execute(
                "DELETE FROM devbrain.curator_re_eval_queue "
                "WHERE memory_id IN "
                "(SELECT id FROM devbrain.memory WHERE project_id = %s) "
                "OR cascade_source_id IN "
                "(SELECT id FROM devbrain.memory WHERE project_id = %s)",
                (pid, pid),
            )
            cur.execute(
                "DELETE FROM devbrain.memory_ledger "
                "WHERE memory_id IN (SELECT id FROM devbrain.memory WHERE project_id = %s)",
                (pid,),
            )
            cur.execute(
                "DELETE FROM devbrain.memory_dependencies "
                "WHERE from_memory_id IN (SELECT id FROM devbrain.memory WHERE project_id = %s) "
                "   OR to_memory_id   IN (SELECT id FROM devbrain.memory WHERE project_id = %s)",
                (pid, pid),
            )
            # end_session_log added in migration 018; gracefully missing
            # in installs that haven't migrated yet (test will SKIP rather
            # than ERROR there).
            try:
                cur.execute(
                    "DELETE FROM devbrain.end_session_log WHERE project_id = %s",
                    (pid,),
                )
            except Exception:
                conn.rollback()
            cur.execute("DELETE FROM devbrain.memory WHERE project_id = %s", (pid,))
            # factory_jobs FK project_id; postulates that route through
            # generate_brief insert real factory_jobs rows (P6/P7), so
            # clean them up before the project. Older postulates that
            # didn't touch this table no-op cleanly here.
            cur.execute(
                "DELETE FROM devbrain.factory_jobs WHERE project_id = %s",
                (pid,),
            )
            cur.execute("DELETE FROM devbrain.projects WHERE id = %s", (pid,))
    conn.commit()


@pytest.fixture
def memory_factory(conn):
    """Insert a devbrain.memory row directly (bypassing the MCP server).

    Optional kwargs `tier`, `strength`, `compliance_profiles` (Atlas
    Step 7a) set those columns at insert time when non-None — older
    postulates that don't pass them get the table defaults.
    """
    def make(
        project_id: str,
        *,
        kind: str = "decision",
        title: str | None = None,
        content: str | None = None,
        provenance_id: str | None = None,
        tier: str | None = None,
        strength: float | None = None,
        compliance_profiles: list[str] | None = None,
    ) -> dict:
        title = title or f"{TEST_TAG}title_{uuid.uuid4().hex[:6]}"
        content = content or f"{TEST_TAG}body_{uuid.uuid4().hex[:6]}"

        cols = ["project_id", "kind", "title", "content", "provenance_id"]
        vals: list = [project_id, kind, title, content, provenance_id]
        if tier is not None:
            cols.append("tier")
            vals.append(tier)
        if strength is not None:
            cols.append("strength")
            vals.append(strength)
        if compliance_profiles is not None:
            cols.append("compliance_profiles")
            vals.append(compliance_profiles)

        placeholders = ", ".join(["%s"] * len(cols))
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO devbrain.memory ({', '.join(cols)}) "
                f"VALUES ({placeholders}) RETURNING id",
                vals,
            )
            row = cur.fetchone()
        conn.commit()
        return {
            "id": row[0],
            "title": title,
            "content": content,
            "kind": kind,
            "tier": tier or "memory",
        }

    return make


@pytest.fixture
def factory_job_factory(conn):
    """Insert a devbrain.factory_jobs row.

    Cleanup happens at fixture teardown — required because the brief
    generator opens its own cursor and SELECTs on committed state, so
    the insert is committed up-front and the conn fixture's rollback
    won't undo it.
    """
    created: list = []

    def make(project_id, spec: str = "test", status: str = "queued",
             title: str | None = None) -> dict:
        title = title or f"{TEST_TAG}job_{uuid.uuid4().hex[:6]}"
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO devbrain.factory_jobs "
                "(project_id, title, spec, status) "
                "VALUES (%s, %s, %s, %s) "
                "RETURNING id, project_id, title, spec, status",
                (project_id, title, spec, status),
            )
            cols = [d[0] for d in cur.description]
            row = dict(zip(cols, cur.fetchone()))
        conn.commit()
        created.append(row["id"])
        return row

    yield make

    if created:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM devbrain.factory_jobs WHERE id = ANY(%s)",
                (created,),
            )
        conn.commit()
