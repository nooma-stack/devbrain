"""Tests for the start_session MCP tool's data path.

The MCP tool lives in mcp-server/src/index.ts; these tests exercise
the schema-level invariants the TS code relies on:

  * A start_session row is just a breadcrumb at seq=0 with
    `is_session_start=true` in applies_when.
  * The chain query (provenance_id + seq order) puts the
    start_session row first.
  * Breadcrumbs after start_session increment seq from 1 upward.
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
    slug = f"start-session-test-{uuid.uuid4().hex[:8]}"
    with live_db.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.projects (slug, name) VALUES (%s, %s) RETURNING id",
            (slug, "start_session test"),
        )
        project_id = cur.fetchone()[0]
    live_db.commit()
    yield {"id": project_id, "slug": slug}
    with live_db.cursor() as cur:
        cur.execute("DELETE FROM devbrain.memory WHERE project_id = %s", (project_id,))
        cur.execute("DELETE FROM devbrain.projects WHERE id = %s", (project_id,))
    live_db.commit()


def _insert_breadcrumb(conn, project_id, conv_uuid, title, content, seq, is_start):
    """Mirror of writeBreadcrumbRow() in mcp-server/src/index.ts."""
    applies_when = {
        "conversation_uuid": conv_uuid,
        "seq": seq,
        "source": "mcp:breadcrumb",
    }
    if is_start:
        applies_when["is_session_start"] = True
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO devbrain.memory
                (project_id, kind, title, content, tier, strength,
                 provenance_id, applies_when)
            VALUES (%s, 'session_breadcrumb', %s, %s, 'memory', 1.0,
                    %s, %s::jsonb)
            RETURNING id
            """,
            (project_id, title, content, conv_uuid, json.dumps(applies_when)),
        )
        return cur.fetchone()[0]


def test_start_session_row_has_is_session_start_marker(live_db, synth_project):
    conv = str(uuid.uuid4())
    _insert_breadcrumb(
        live_db, synth_project["id"], conv,
        "User wants Phase 8 done", "Build fan-out + breadcrumb tooling",
        seq=0, is_start=True,
    )
    live_db.commit()
    with live_db.cursor() as cur:
        cur.execute(
            """
            SELECT applies_when->'is_session_start', applies_when->>'seq'
            FROM devbrain.memory
            WHERE provenance_id = %s AND kind = 'session_breadcrumb'
            """,
            (conv,),
        )
        is_start, seq = cur.fetchone()
    assert is_start is True
    assert seq == "0"


def test_chain_query_orders_start_session_first(live_db, synth_project):
    conv = str(uuid.uuid4())
    _insert_breadcrumb(live_db, synth_project["id"], conv,
                       "intent", "user wants X", seq=0, is_start=True)
    _insert_breadcrumb(live_db, synth_project["id"], conv,
                       "milestone 1", "did Y", seq=1, is_start=False)
    _insert_breadcrumb(live_db, synth_project["id"], conv,
                       "milestone 2", "did Z", seq=2, is_start=False)
    live_db.commit()
    with live_db.cursor() as cur:
        cur.execute(
            """
            SELECT title, (applies_when->>'seq')::int AS s,
                   (applies_when->'is_session_start')::bool AS is_start
            FROM devbrain.memory
            WHERE provenance_id = %s AND kind = 'session_breadcrumb'
            ORDER BY (applies_when->>'seq')::int
            """,
            (conv,),
        )
        rows = cur.fetchall()
    assert [r[0] for r in rows] == ["intent", "milestone 1", "milestone 2"]
    assert [r[1] for r in rows] == [0, 1, 2]
    # Only seq=0 is the session start.
    assert rows[0][2] is True
    assert rows[1][2] is None  # absent in applies_when
    assert rows[2][2] is None


def test_breadcrumb_chain_index_covers_query(live_db):
    """Sanity: the index from migration 040 is what the chain query uses."""
    with live_db.cursor() as cur:
        cur.execute(
            "EXPLAIN SELECT id FROM devbrain.memory "
            "WHERE provenance_id = '00000000-0000-0000-0000-000000000000'::uuid "
            "  AND kind = 'session_breadcrumb' "
            "  AND archived_at IS NULL "
            "ORDER BY created_at"
        )
        plan = "\n".join(r[0] for r in cur.fetchall())
    assert "idx_memory_breadcrumb_chain" in plan, (
        f"chain index not used by query planner:\n{plan}"
    )
