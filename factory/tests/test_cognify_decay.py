"""Integration tests for cognify_decay pass.

Covers:
  - Decay is applied only to idle rows (>= 30 days idle)
  - 30-day tier: strength * 0.5
  - 90-day tier: strength * 0.12
  - Decay is monotonic (never increases strength)
  - Archived rows are not decayed
  - Dry-run returns count without mutating
  - project_id scoping: decay in project A doesn't affect project B
"""
from __future__ import annotations

import uuid

import pytest

from cognify.decay import DecayPass, DECAY_30D_MULTIPLIER, DECAY_90D_MULTIPLIER, MIN_STRENGTH


def _seed_strength(conn, memory_id, strength):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET strength = %s WHERE id = %s",
            (strength, memory_id),
        )
    conn.commit()


def _set_idle(conn, memory_id, days):
    """Set last_cascade_at, last_hit, and created_at to simulate idleness."""
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


def _read_strength(conn, memory_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT strength FROM devbrain.memory WHERE id = %s",
            (memory_id,),
        )
        return float(cur.fetchone()[0])


@pytest.mark.db
def test_decay_leaves_fresh_rows_untouched(conn, project_factory, memory_factory):
    """Rows with last_cascade_at within 30 days are not decayed."""
    project = project_factory("decay_fresh")
    m = memory_factory(project["id"])
    _seed_strength(conn, m["id"], 1.0)
    # Set idle to 5 days (below threshold)
    _set_idle(conn, m["id"], 5)

    pass_ = DecayPass()
    result = pass_.run(conn, project["id"])

    assert _read_strength(conn, m["id"]) == pytest.approx(1.0, abs=0.001)
    assert result.rows_processed == 0


@pytest.mark.db
def test_decay_applies_30d_tier(conn, project_factory, memory_factory):
    """Rows idle >= 30 days (but < 90 days) get 50% decay."""
    project = project_factory("decay_30d")
    m = memory_factory(project["id"])
    _seed_strength(conn, m["id"], 1.0)
    _set_idle(conn, m["id"], 45)

    pass_ = DecayPass()
    result = pass_.run(conn, project["id"])

    new_strength = _read_strength(conn, m["id"])
    assert new_strength == pytest.approx(DECAY_30D_MULTIPLIER, abs=0.01)
    assert result.rows_processed == 1


@pytest.mark.db
def test_decay_applies_90d_tier(conn, project_factory, memory_factory):
    """Rows idle >= 90 days get 12% decay (not 50%)."""
    project = project_factory("decay_90d")
    m = memory_factory(project["id"])
    _seed_strength(conn, m["id"], 1.0)
    _set_idle(conn, m["id"], 100)

    pass_ = DecayPass()
    result = pass_.run(conn, project["id"])

    new_strength = _read_strength(conn, m["id"])
    assert new_strength == pytest.approx(DECAY_90D_MULTIPLIER, abs=0.01)
    assert result.rows_processed == 1


@pytest.mark.db
def test_decay_monotonic_never_increases(conn, project_factory, memory_factory):
    """Decay never increases strength. Running twice produces strength <= first run."""
    project = project_factory("decay_mono")
    m = memory_factory(project["id"])
    _seed_strength(conn, m["id"], 0.8)
    _set_idle(conn, m["id"], 45)

    pass_ = DecayPass()
    pass_.run(conn, project["id"])
    after_first = _read_strength(conn, m["id"])

    pass_.run(conn, project["id"])
    after_second = _read_strength(conn, m["id"])

    assert after_second <= after_first
    assert after_second >= MIN_STRENGTH


@pytest.mark.db
def test_decay_skips_archived_rows(conn, project_factory, memory_factory):
    """Archived rows (archived_at IS NOT NULL) are not decayed."""
    project = project_factory("decay_arch")
    m = memory_factory(project["id"])
    _seed_strength(conn, m["id"], 1.0)
    _set_idle(conn, m["id"], 100)
    # Archive the row
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET archived_at = now() WHERE id = %s",
            (m["id"],),
        )
    conn.commit()

    pass_ = DecayPass()
    pass_.run(conn, project["id"])

    strength = _read_strength(conn, m["id"])
    assert strength == pytest.approx(1.0, abs=0.001)


@pytest.mark.db
def test_decay_dry_run_no_mutation(conn, project_factory, memory_factory):
    """Dry run returns candidate count but doesn't mutate strength."""
    project = project_factory("decay_dry")
    m = memory_factory(project["id"])
    _seed_strength(conn, m["id"], 1.0)
    _set_idle(conn, m["id"], 45)

    pass_ = DecayPass()
    result = pass_.run(conn, project["id"], dry_run=True)

    assert result.rows_processed == 0
    assert result.metadata.get("dry_run_would_process", 0) >= 1
    # Strength unchanged
    assert _read_strength(conn, m["id"]) == pytest.approx(1.0, abs=0.001)


@pytest.mark.db
def test_decay_project_scoped(conn, project_factory, memory_factory):
    """Decay on project A does not affect idle rows in project B."""
    proj_a = project_factory("decay_scopea")
    proj_b = project_factory("decay_scopeb")
    m_a = memory_factory(proj_a["id"])
    m_b = memory_factory(proj_b["id"])
    for mid in (m_a["id"], m_b["id"]):
        _seed_strength(conn, mid, 1.0)
        _set_idle(conn, mid, 45)

    pass_ = DecayPass()
    pass_.run(conn, proj_a["id"])

    assert _read_strength(conn, m_a["id"]) < 1.0
    assert _read_strength(conn, m_b["id"]) == pytest.approx(1.0, abs=0.001)
