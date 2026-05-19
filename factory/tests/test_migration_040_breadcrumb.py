"""Tests for migration 040 — session_breadcrumb kind + chain index.

The MCP tool itself (breadcrumb) lives in mcp-server/src/index.ts;
these tests exercise the DB substrate it sits on top of:
  * kind='session_breadcrumb' is accepted by memory_kind_check
  * The partial chain index speeds up conversation-grouped reads
  * INSERT shape matches what the TS tool emits (provenance_id =
    conversation_uuid, applies_when carries seq + conversation_uuid)
  * Multiple breadcrumbs sharing a provenance_id coexist (no unique
    collision against migration 037/039's session-summary indexes)
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import psycopg2
import pytest

_FACTORY = Path(__file__).resolve().parents[1]
if str(_FACTORY) not in sys.path:
    sys.path.insert(0, str(_FACTORY))


def _live_conn():
    from config import DATABASE_URL  # noqa: PLC0415
    return psycopg2.connect(DATABASE_URL)


@pytest.fixture
def live_db():
    if not os.environ.get("DEVBRAIN_DB_PASSWORD") and not os.environ.get("DEVBRAIN_TEST_DATABASE_URL"):
        pytest.skip("DB not configured for tests")
    conn = _live_conn()
    yield conn
    conn.close()


@pytest.fixture
def synth_project(live_db):
    """Disposable project. Removed at teardown."""
    slug = f"breadcrumb-test-{uuid.uuid4().hex[:8]}"
    with live_db.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.projects (slug, name) VALUES (%s, %s) RETURNING id",
            (slug, "Breadcrumb test"),
        )
        project_id = cur.fetchone()[0]
    live_db.commit()
    yield {"id": project_id, "slug": slug}
    with live_db.cursor() as cur:
        cur.execute("DELETE FROM devbrain.memory WHERE project_id = %s", (project_id,))
        cur.execute("DELETE FROM devbrain.projects WHERE id = %s", (project_id,))
    live_db.commit()


def test_kind_check_accepts_session_breadcrumb(live_db, synth_project):
    """Inserting kind='session_breadcrumb' succeeds — migration 040 added it."""
    with live_db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO devbrain.memory
                (project_id, kind, title, content, tier, strength, provenance_id)
            VALUES (%s, 'session_breadcrumb', 'milestone-1', 'noted it', 'memory', 1.0, %s)
            RETURNING id
            """,
            (synth_project["id"], str(uuid.uuid4())),
        )
        new_id = cur.fetchone()[0]
    live_db.commit()
    assert new_id is not None


def test_kind_check_still_rejects_unknown_kind(live_db, synth_project):
    """Sanity: only the documented kinds are valid."""
    with pytest.raises(psycopg2.errors.CheckViolation):
        with live_db.cursor() as cur:
            cur.execute(
                """
                INSERT INTO devbrain.memory
                    (project_id, kind, title, content, tier, strength)
                VALUES (%s, 'fake-kind', 't', 'c', 'memory', 1.0)
                """,
                (synth_project["id"],),
            )
    live_db.rollback()


def test_multiple_breadcrumbs_share_provenance_id(live_db, synth_project):
    """A conversation's breadcrumbs share provenance_id without colliding
    against the session_summary unique index."""
    conv_uuid = str(uuid.uuid4())
    with live_db.cursor() as cur:
        for i in range(1, 4):
            cur.execute(
                """
                INSERT INTO devbrain.memory
                    (project_id, kind, title, content, tier, strength,
                     provenance_id, applies_when)
                VALUES (%s, 'session_breadcrumb', %s, %s, 'memory', 1.0,
                        %s, %s::jsonb)
                """,
                (
                    synth_project["id"],
                    f"step-{i}",
                    f"breadcrumb {i}",
                    conv_uuid,
                    json.dumps({"conversation_uuid": conv_uuid, "seq": i,
                                "source": "mcp:breadcrumb"}),
                ),
            )
    live_db.commit()

    with live_db.cursor() as cur:
        cur.execute(
            """
            SELECT title, applies_when->>'seq' AS seq
            FROM devbrain.memory
            WHERE provenance_id = %s AND kind = 'session_breadcrumb'
            ORDER BY (applies_when->>'seq')::int
            """,
            (conv_uuid,),
        )
        rows = cur.fetchall()
    assert [r[0] for r in rows] == ["step-1", "step-2", "step-3"]
    assert [int(r[1]) for r in rows] == [1, 2, 3]


def test_breadcrumb_chain_index_exists(live_db):
    """Migration 040 created the partial index used by the chain query."""
    with live_db.cursor() as cur:
        cur.execute(
            """
            SELECT indexdef FROM pg_indexes
            WHERE schemaname='devbrain'
              AND indexname='idx_memory_breadcrumb_chain'
            """,
        )
        row = cur.fetchone()
    assert row is not None
    indexdef = row[0]
    assert "session_breadcrumb" in indexdef
    assert "provenance_id" in indexdef
    assert "created_at" in indexdef
    assert "archived_at IS NULL" in indexdef
