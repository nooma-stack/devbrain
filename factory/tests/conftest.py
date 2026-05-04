"""Shared pytest fixtures for factory/ tests.

Two responsibilities:

1. sys.path tweak so factory's modules resolve when pytest is invoked as
   `cd factory && pytest tests/...` (the rootdir convention used by CI).
2. DB fixtures (`database_url`, `conn`) for tests gated on
   `@pytest.mark.db`. These mirror the fixtures in
   tests/postulates/conftest.py so postulate-tests and factory-tests use
   identical connection semantics, but live here so pytest discovers them
   when run from the factory/ rootdir.

DB-using tests skip cleanly when DEVBRAIN_DB_PASSWORD (or
DEVBRAIN_TEST_DATABASE_URL) is not set, which is the case in the no-DB
CI subset — see .github/workflows/test.yml.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Add factory dir to path so imports work
sys.path.insert(0, str(Path(__file__).parent.parent))


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
