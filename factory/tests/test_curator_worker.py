"""Integration tests for the cascade worker.

Drains rows from devbrain.curator_re_eval_queue and applies the bounded
additive cascade penalty (defined in factory/curator/strength.py) to the
target memory's strength column.

These tests use real Postgres (devbrain-db). They commit mid-test to set
up state visible to a subsequent SELECT FOR UPDATE call from inside the
worker. The conftest in factory/tests/conftest.py rolls back any
uncommitted work at teardown — committed rows are owned by the
project_factory / memory_factory fixtures (parallel to those in
tests/postulates/conftest.py) and cleaned up at fixture teardown.
"""
from __future__ import annotations

import pytest

from curator.strength import PENALTY  # noqa: F401  (referenced in commentary)
from curator.worker import drain_one_batch


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
        worker_mod,
        "apply_cascade",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
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
