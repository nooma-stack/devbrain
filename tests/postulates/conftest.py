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
    """Per-test connection with autocommit OFF so cleanup is total."""
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
    """Create disposable projects with a unique slug. Cleaned up at teardown."""
    created: list[str] = []

    def make(slug_hint: str = "p") -> dict:
        slug = f"{TEST_TAG}{slug_hint}_{uuid.uuid4().hex[:8]}"
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO devbrain.projects (slug, name) VALUES (%s, %s) "
                "RETURNING id, slug",
                (slug, f"Postulate Test {slug_hint}"),
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

    # Order: ledger rows first (no FK), then deps, then memory, then project.
    with conn.cursor() as cur:
        for pid in created:
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
            cur.execute("DELETE FROM devbrain.memory WHERE project_id = %s", (pid,))
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
                "INSERT INTO devbrain.memory (project_id, kind, title, content, provenance_id) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (project_id, kind, title, content, provenance_id),
            )
            row = cur.fetchone()
        conn.commit()
        return {"id": row[0], "title": title, "content": content, "kind": kind}

    return make
