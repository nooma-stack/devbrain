"""Shared pytest fixtures for factory/ tests.

Three responsibilities:

1. sys.path tweak so factory's modules resolve when pytest is invoked as
   `cd factory && pytest tests/...` (the rootdir convention used by CI).
2. DB fixtures (`database_url`, `conn`) for tests gated on
   `@pytest.mark.db`. These mirror the fixtures in
   tests/postulates/conftest.py so postulate-tests and factory-tests use
   identical connection semantics, but live here so pytest discovers them
   when run from the factory/ rootdir.
3. Row-factory fixtures (`project_factory`, `memory_factory`) parallel
   to those in tests/postulates/conftest.py. Tests that commit mid-test
   (e.g. cascade-worker integration tests that need to set up state
   visible to a subsequent SELECT FOR UPDATE) need explicit cleanup —
   these factories track inserted IDs and delete them at teardown.

DB-using tests skip cleanly when DEVBRAIN_DB_PASSWORD (or
DEVBRAIN_TEST_DATABASE_URL) is not set, which is the case in the no-DB
CI subset — see .github/workflows/test.yml.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

# Add factory dir to path so imports work
sys.path.insert(0, str(Path(__file__).parent.parent))

# Every row inserted by a factory fixture is tagged with this prefix so
# the cleanup is easy to spot and won't touch real data even if the
# delete query is buggy.
TEST_TAG = "factory_test_"


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
            "DB-marked tests require a real Postgres."
        )
    return f"postgresql://{user}:{password}@{host}:{port}/{name}"


@pytest.fixture(scope="session")
def database_url() -> str:
    return _database_url()


@pytest.fixture
def conn(database_url):
    """Per-test connection. Caller is responsible for cleanup of any rows
    they insert; schema-assertion tests that only read information_schema
    don't need teardown.
    """
    psycopg2 = pytest.importorskip("psycopg2")
    c = psycopg2.connect(database_url)
    try:
        with c.cursor() as cur:
            cur.execute("SET devbrain.actor = 'factory-test'")
        c.commit()
        yield c
    finally:
        c.rollback()
        c.close()


@pytest.fixture
def project_factory(conn):
    """Create disposable projects with a unique slug. Cleaned up at teardown.

    Cleanup deletes every row anchored to a created project_id (memory
    rows, dependency edges, ledger rows, curator queue rows, factory_jobs
    rows) before the project itself — required because committed rows
    are not rolled back by the conn fixture.
    """
    created: list[str] = []

    def make(slug_hint: str = "p") -> dict:
        slug = f"{TEST_TAG}{slug_hint}_{uuid.uuid4().hex[:8]}"
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO devbrain.projects (slug, name) VALUES (%s, %s) "
                "RETURNING id, slug",
                (slug, f"Factory Test {slug_hint}"),
            )
            row = cur.fetchone()
        conn.commit()
        created.append(row[0])
        return {"id": row[0], "slug": row[1]}

    yield make

    # The test itself may have left the connection in an aborted-
    # transaction state. Roll back before cleanup so DELETE statements run.
    conn.rollback()

    with conn.cursor() as cur:
        for pid in created:
            # Curator queue rows reference memory.id with ON DELETE CASCADE
            # for memory_id, but cascade_source_id is plain REFERENCES — so
            # we have to drop queue rows explicitly before deleting memory.
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
                "WHERE memory_id IN "
                "(SELECT id FROM devbrain.memory WHERE project_id = %s)",
                (pid,),
            )
            cur.execute(
                "DELETE FROM devbrain.memory_dependencies "
                "WHERE from_memory_id IN "
                "(SELECT id FROM devbrain.memory WHERE project_id = %s) "
                "OR to_memory_id IN "
                "(SELECT id FROM devbrain.memory WHERE project_id = %s)",
                (pid, pid),
            )
            cur.execute(
                "DELETE FROM devbrain.memory WHERE project_id = %s", (pid,)
            )
            cur.execute(
                "DELETE FROM devbrain.factory_jobs WHERE project_id = %s",
                (pid,),
            )
            cur.execute("DELETE FROM devbrain.projects WHERE id = %s", (pid,))
    conn.commit()


@pytest.fixture
def memory_factory(conn):
    """Insert a devbrain.memory row directly (bypassing the MCP server)."""

    def make(
        project_id: str,
        *,
        kind: str = "decision",
        title: str | None = None,
        content: str | None = None,
        provenance_id: str | None = None,
    ) -> dict:
        title = title or f"{TEST_TAG}title_{uuid.uuid4().hex[:6]}"
        content = content or f"{TEST_TAG}body_{uuid.uuid4().hex[:6]}"
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO devbrain.memory "
                "(project_id, kind, title, content, provenance_id) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (project_id, kind, title, content, provenance_id),
            )
            row = cur.fetchone()
        conn.commit()
        return {"id": row[0], "title": title, "content": content, "kind": kind}

    return make
