"""Integration tests for cognify_gc pass.

Covers:
  - Low-strength orphan rows >= 90 days idle are archived
  - archive = set archived_at; never DELETE (HIPAA)
  - Rows with outgoing edges are NOT archived (not orphans)
  - Rows with strength >= threshold are NOT archived
  - Rows idle < 90 days are NOT archived
  - Dry-run returns count without archiving
  - project_id scoping
"""
from __future__ import annotations

import pytest

from cognify.gc import GCPass, GC_STRENGTH_THRESHOLD


def _set_strength(conn, memory_id, strength):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET strength = %s WHERE id = %s",
            (strength, memory_id),
        )
    conn.commit()


def _set_idle(conn, memory_id, days):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory "
            "SET last_cascade_at = NOW() - INTERVAL '" + str(days) + " days', "
            "    last_hit = NOW() - INTERVAL '" + str(days) + " days', "
            "    created_at = NOW() - INTERVAL '" + str(days) + " days' "
            "WHERE id = %s",
            (memory_id,),
        )
    conn.commit()


def _is_archived(conn, memory_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT archived_at IS NOT NULL FROM devbrain.memory WHERE id = %s",
            (memory_id,),
        )
        return cur.fetchone()[0]


def _add_edge(conn, from_id, to_id):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.memory_dependencies "
            "(from_memory_id, to_memory_id, edge_type) "
            "VALUES (%s, %s, 'depends_on') ON CONFLICT DO NOTHING",
            (from_id, to_id),
        )
    conn.commit()


@pytest.mark.db
def test_gc_archives_low_strength_idle_orphan(conn, project_factory, memory_factory):
    """An orphan row with low strength and >= 90 days idle is archived."""
    project = project_factory("gc_basic")
    m = memory_factory(project["id"])
    _set_strength(conn, m["id"], 0.05)
    _set_idle(conn, m["id"], 100)

    pass_ = GCPass()
    result = pass_.run(conn, project["id"])

    assert _is_archived(conn, m["id"])
    assert result.rows_processed >= 1
    assert result.metadata.get("archived_count", 0) >= 1


@pytest.mark.db
def test_gc_never_deletes_rows(conn, project_factory, memory_factory):
    """GC sets archived_at — it never deletes the row. Count stays the same."""
    project = project_factory("gc_nodelete")
    m = memory_factory(project["id"])
    _set_strength(conn, m["id"], 0.05)
    _set_idle(conn, m["id"], 100)

    before_count = 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory WHERE id = %s", (m["id"],)
        )
        before_count = cur.fetchone()[0]

    pass_ = GCPass()
    pass_.run(conn, project["id"])

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM devbrain.memory WHERE id = %s", (m["id"],)
        )
        after_count = cur.fetchone()[0]

    assert before_count == 1
    assert after_count == 1  # Row still exists, just archived
    assert _is_archived(conn, m["id"])


@pytest.mark.db
def test_gc_skips_rows_with_outgoing_edges(conn, project_factory, memory_factory):
    """Rows with outgoing edges are not orphans and should not be archived."""
    project = project_factory("gc_edges")
    m_src = memory_factory(project["id"])
    m_tgt = memory_factory(project["id"])
    _set_strength(conn, m_src["id"], 0.05)
    _set_idle(conn, m_src["id"], 100)
    _add_edge(conn, m_src["id"], m_tgt["id"])

    pass_ = GCPass()
    pass_.run(conn, project["id"])

    assert not _is_archived(conn, m_src["id"])


@pytest.mark.db
def test_gc_skips_high_strength_rows(conn, project_factory, memory_factory):
    """Rows with strength >= threshold are not archived regardless of age."""
    project = project_factory("gc_highstr")
    m = memory_factory(project["id"])
    _set_strength(conn, m["id"], GC_STRENGTH_THRESHOLD + 0.1)
    _set_idle(conn, m["id"], 100)

    pass_ = GCPass()
    pass_.run(conn, project["id"])

    assert not _is_archived(conn, m["id"])


@pytest.mark.db
def test_gc_skips_recently_active_rows(conn, project_factory, memory_factory):
    """Rows idle < 90 days are not archived even if low-strength orphans."""
    project = project_factory("gc_recent")
    m = memory_factory(project["id"])
    _set_strength(conn, m["id"], 0.05)
    _set_idle(conn, m["id"], 30)  # Only 30 days idle

    pass_ = GCPass()
    pass_.run(conn, project["id"])

    assert not _is_archived(conn, m["id"])


@pytest.mark.db
def test_gc_dry_run_no_archive(conn, project_factory, memory_factory):
    """Dry run reports candidates without setting archived_at."""
    project = project_factory("gc_dry")
    m = memory_factory(project["id"])
    _set_strength(conn, m["id"], 0.05)
    _set_idle(conn, m["id"], 100)

    pass_ = GCPass()
    result = pass_.run(conn, project["id"], dry_run=True)

    assert result.rows_processed == 0
    assert result.metadata.get("dry_run_would_archive", 0) >= 1
    assert not _is_archived(conn, m["id"])


@pytest.mark.db
def test_gc_project_scoped(conn, project_factory, memory_factory):
    """GC on project A does not archive orphans in project B."""
    proj_a = project_factory("gc_scopea")
    proj_b = project_factory("gc_scopeb")
    m_a = memory_factory(proj_a["id"])
    m_b = memory_factory(proj_b["id"])
    for mid in (m_a["id"], m_b["id"]):
        _set_strength(conn, mid, 0.05)
        _set_idle(conn, mid, 100)

    pass_ = GCPass()
    pass_.run(conn, proj_a["id"])

    assert _is_archived(conn, m_a["id"])
    assert not _is_archived(conn, m_b["id"])
