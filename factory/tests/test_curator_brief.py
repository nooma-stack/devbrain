"""Integration tests for the curator brief generator.

generate_brief() runs synchronously at the QUEUED -> PLANNING state
transition. It assembles a CuratorBrief snapshot from the substrate
(memory rows, last_cascade_at audit) and persists it to
factory_jobs.curator_brief JSONB so every downstream phase reads an
identical view.

These tests use a real Postgres (devbrain-db). They commit mid-test
because generate_brief opens its own cursors and SELECTs on committed
state.
"""
from __future__ import annotations

import pytest

from curator.brief import generate_brief


@pytest.mark.db
@pytest.mark.skip(
    reason="TODO Step 7: re-enable when compliance_profiles + "
    "compliance_profiles_enabled columns ship. brief.py already handles "
    "the missing-column case gracefully (try/except around the SELECT)."
)
def test_brief_filters_by_compliance_profiles(
    conn, project_factory, memory_factory, factory_job_factory
):
    project = project_factory("bf", compliance_profiles_enabled=["hipaa"])
    rule_hipaa = memory_factory(
        project["id"], kind="decision", tier="rule",
        content="HIPAA rule", compliance_profiles=["hipaa"],
    )
    rule_soc2 = memory_factory(
        project["id"], kind="decision", tier="rule",
        content="SOC2 rule", compliance_profiles=["soc2"],
    )
    job = factory_job_factory(project["id"], spec="touches phi_log.py")

    brief = generate_brief(conn, job["id"], project["id"], job["spec"])

    rule_ids = {r.id for r in brief.rules}
    assert rule_hipaa["id"] in rule_ids
    assert rule_soc2["id"] not in rule_ids


@pytest.mark.db
def test_brief_excludes_archived(
    conn, project_factory, memory_factory, factory_job_factory
):
    project = project_factory("bea")
    m = memory_factory(project["id"], tier="lesson", content="archived me")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET archived_at = NOW() WHERE id = %s",
            (m["id"],),
        )
    conn.commit()
    job = factory_job_factory(project["id"], spec="anything")

    brief = generate_brief(conn, job["id"], project["id"], job["spec"])
    assert m["id"] not in {r.id for r in brief.lessons}


@pytest.mark.db
def test_brief_persisted_to_factory_job(
    conn, project_factory, factory_job_factory
):
    project = project_factory("bp")
    job = factory_job_factory(project["id"], spec="x")

    brief = generate_brief(conn, job["id"], project["id"], job["spec"])

    with conn.cursor() as cur:
        cur.execute(
            "SELECT curator_brief FROM devbrain.factory_jobs WHERE id = %s",
            (job["id"],),
        )
        stored = cur.fetchone()[0]
    assert stored is not None
    assert stored["version"] == "1.0"
    assert stored["job_id"] == str(brief.job_id)


@pytest.mark.db
def test_brief_includes_recent_cascade_signals(
    conn, project_factory, memory_factory, factory_job_factory
):
    project = project_factory("brc")
    m_dep = memory_factory(project["id"], tier="lesson", content="recently cascaded")
    memory_factory(project["id"], content="source")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET last_cascade_at = NOW() WHERE id = %s",
            (m_dep["id"],),
        )
    conn.commit()
    job = factory_job_factory(project["id"], spec="x")

    brief = generate_brief(conn, job["id"], project["id"], job["spec"])
    affected = {n.affected_memory_id for n in brief.recent_cascade_signals}
    assert m_dep["id"] in affected


@pytest.mark.db
def test_state_machine_transition_queued_to_planning_generates_brief(
    conn, database_url, project_factory, factory_job_factory
):
    """End-to-end: FactoryDB.transition(QUEUED -> PLANNING) writes a
    brief into factory_jobs.curator_brief. The brief generation hook in
    state_machine fires only on this specific edge.
    """
    from state_machine import FactoryDB, JobStatus

    project = project_factory("smb")
    job = factory_job_factory(
        project["id"], spec="implement greeting", status="queued"
    )

    db = FactoryDB(database_url)
    transitioned = db.transition(str(job["id"]), JobStatus.PLANNING)
    assert transitioned.status == JobStatus.PLANNING

    with conn.cursor() as cur:
        cur.execute(
            "SELECT curator_brief FROM devbrain.factory_jobs WHERE id = %s",
            (job["id"],),
        )
        stored = cur.fetchone()[0]
    assert stored is not None
    assert stored["version"] == "1.0"
    assert stored["job_id"] == str(job["id"])
