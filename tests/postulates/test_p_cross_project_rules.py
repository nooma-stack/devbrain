"""P_cross_project_rules — Rules in the canonical 'devbrain' project surface
across project boundaries when the profile intersection holds.

POSTULATE
---------
A tier='rule' row stored under the canonical 'devbrain' project (the
seeded regulatory library; see migration 023) appears in a non-devbrain
project's curator brief when both:
  1. the target project has compliance_profiles_enabled overlapping the
     rule's compliance_profiles, and
  2. the rule is not archived.

This is the narrow, deliberate exception to P3's same-project memory
isolation invariant. It is restricted to rules with non-empty
compliance_profiles and to the single canonical 'devbrain' project_id;
no other cross-project surface is permitted by this postulate. Lessons,
decisions, and cascade signals remain project-local.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "factory"))

from curator.brief import generate_brief  # noqa: E402


def _canonical_devbrain_project_id(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.projects WHERE slug = 'devbrain'"
        )
        row = cur.fetchone()
    assert row is not None, (
        "canonical 'devbrain' project must exist — fresh installs should "
        "have it from migration 001 + slug seeding"
    )
    return row[0]


def _drop_memory(conn, memory_id):
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM devbrain.memory_ledger WHERE memory_id = %s",
            (memory_id,),
        )
        cur.execute(
            "DELETE FROM devbrain.memory WHERE id = %s", (memory_id,)
        )
    conn.commit()


def test_canonical_devbrain_rules_surface_in_other_project_brief(
    conn, memory_factory, project_factory, factory_job_factory
):
    """A HIPAA rule seeded under the canonical 'devbrain' project must
    appear in a non-canonical project's brief when that project enables
    'hipaa'."""
    canonical_id = _canonical_devbrain_project_id(conn)
    library_rule = memory_factory(
        canonical_id, kind="decision", tier="rule",
        content="cross-project library rule",
        compliance_profiles=["hipaa"],
    )
    try:
        target = project_factory(
            "cross_target", compliance_profiles_enabled=["hipaa"]
        )
        job = factory_job_factory(target["id"], spec="touches phi_log.py")
        brief = generate_brief(conn, job["id"], target["id"], job["spec"])

        rule_ids = {r.id for r in brief.rules}
        assert library_rule["id"] in rule_ids, (
            "canonical-project HIPAA rule must surface in a project that "
            "enables 'hipaa'"
        )
    finally:
        _drop_memory(conn, library_rule["id"])


def test_canonical_devbrain_rules_filtered_by_profile(
    conn, memory_factory, project_factory, factory_job_factory
):
    """A SOC2-only rule in the canonical project does NOT surface in a
    project that only enables 'hipaa'. Cross-project surfacing still
    respects the compliance_profiles intersection."""
    canonical_id = _canonical_devbrain_project_id(conn)
    soc2_rule = memory_factory(
        canonical_id, kind="decision", tier="rule",
        content="canonical SOC2 rule",
        compliance_profiles=["soc2"],
    )
    try:
        target = project_factory(
            "cross_filter", compliance_profiles_enabled=["hipaa"]
        )
        job = factory_job_factory(target["id"], spec="anything")
        brief = generate_brief(conn, job["id"], target["id"], job["spec"])

        rule_ids = {r.id for r in brief.rules}
        assert soc2_rule["id"] not in rule_ids, (
            "SOC2-tagged rule must not surface for a project that only "
            "enabled 'hipaa', even when the rule lives in the canonical project"
        )
    finally:
        _drop_memory(conn, soc2_rule["id"])


def test_canonical_lookup_no_double_surface_when_target_is_canonical(
    conn, memory_factory, factory_job_factory
):
    """When the brief target IS the canonical 'devbrain' project, the
    UNION must not produce duplicate rule rows. Each rule appears exactly
    once in brief.rules."""
    canonical_id = _canonical_devbrain_project_id(conn)

    # Snapshot + temporarily enable 'hipaa' on the canonical project so
    # the brief generator has profiles to filter against. Restore in
    # finally even on failure.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT compliance_profiles_enabled "
            "FROM devbrain.projects WHERE id = %s",
            (canonical_id,),
        )
        prior = cur.fetchone()[0]
        cur.execute(
            "UPDATE devbrain.projects SET compliance_profiles_enabled = %s "
            "WHERE id = %s",
            (["hipaa"], canonical_id),
        )
    conn.commit()

    rule = None
    try:
        rule = memory_factory(
            canonical_id, kind="decision", tier="rule",
            content="self-target rule", compliance_profiles=["hipaa"],
        )
        job = factory_job_factory(canonical_id, spec="anything")
        brief = generate_brief(conn, job["id"], canonical_id, job["spec"])

        matches = [r for r in brief.rules if r.id == rule["id"]]
        assert len(matches) == 1, (
            "rule must appear exactly once when target project IS canonical"
        )
    finally:
        if rule is not None:
            _drop_memory(conn, rule["id"])
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE devbrain.projects SET compliance_profiles_enabled = %s "
                "WHERE id = %s",
                (prior, canonical_id),
            )
        conn.commit()
