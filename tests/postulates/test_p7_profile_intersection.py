"""P7 — Profile intersection: project enabling 'hipaa' sees only HIPAA-tagged rules.

POSTULATE
---------
The curator brief's `rules` section for a project is the set of memories
where tier='rule' AND archived_at IS NULL AND
compliance_profiles && project.compliance_profiles_enabled.

This is one of two verification gates for Phase 7a (the other is P6).
"""
from __future__ import annotations

import sys
from pathlib import Path

# The postulate suite runs from repo root; the curator module lives under
# factory/. Add factory/ to sys.path so the import resolves regardless of
# where pytest is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "factory"))

from curator.brief import generate_brief  # noqa: E402


def test_brief_filters_by_profile_intersection(
    conn, project_factory, memory_factory, factory_job_factory
):
    project = project_factory("p7", compliance_profiles_enabled=["hipaa"])

    rule_hipaa = memory_factory(
        project["id"], kind="decision", tier="rule",
        content="HIPAA-only rule",
    )
    rule_soc2 = memory_factory(
        project["id"], kind="decision", tier="rule",
        content="SOC2-only rule",
    )
    rule_both = memory_factory(
        project["id"], kind="decision", tier="rule",
        content="Both HIPAA and SOC2",
    )

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET compliance_profiles = %s WHERE id = %s",
            (["hipaa"], rule_hipaa["id"]),
        )
        cur.execute(
            "UPDATE devbrain.memory SET compliance_profiles = %s WHERE id = %s",
            (["soc2"], rule_soc2["id"]),
        )
        cur.execute(
            "UPDATE devbrain.memory SET compliance_profiles = %s WHERE id = %s",
            (["hipaa", "soc2"], rule_both["id"]),
        )
    conn.commit()

    job = factory_job_factory(project["id"], spec="touches phi_log.py")
    brief = generate_brief(conn, job["id"], project["id"], job["spec"])

    rule_ids = {r.id for r in brief.rules}
    assert rule_hipaa["id"] in rule_ids
    assert rule_soc2["id"] not in rule_ids
    assert rule_both["id"] in rule_ids
