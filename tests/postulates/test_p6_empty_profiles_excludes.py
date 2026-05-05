"""P6 — Rules with empty compliance_profiles are NOT included in any project's brief.

POSTULATE
---------
A tier='rule' row with compliance_profiles = NULL or [] does NOT appear in
any project's curator brief.rules — explicit opt-in semantics. Projects
only get rules they tagged for.

This is one of two verification gates for Phase 7a (the other is P7).
"""
from __future__ import annotations

import sys
from pathlib import Path

# The postulate suite runs from repo root; the curator module lives under
# factory/. Add factory/ to sys.path so the import resolves regardless of
# where pytest is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "factory"))

from curator.brief import generate_brief  # noqa: E402


def test_null_compliance_profiles_excluded_from_brief(
    conn, project_factory, memory_factory, factory_job_factory
):
    project = project_factory("p6_null", compliance_profiles_enabled=["hipaa"])
    rule_null = memory_factory(
        project["id"], kind="decision", tier="rule",
        content="rule with NULL profiles",
    )
    # NULL profiles by default — memory_factory doesn't set compliance_profiles
    job = factory_job_factory(project["id"], spec="anything")

    brief = generate_brief(conn, job["id"], project["id"], job["spec"])

    assert rule_null["id"] not in {r.id for r in brief.rules}


def test_empty_compliance_profiles_excluded_from_brief(
    conn, project_factory, memory_factory, factory_job_factory
):
    project = project_factory("p6_empty", compliance_profiles_enabled=["hipaa"])
    rule_empty = memory_factory(
        project["id"], kind="decision", tier="rule",
        content="rule with empty profiles",
    )
    # Explicitly set to empty array
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.memory SET compliance_profiles = '{}' WHERE id = %s",
            (rule_empty["id"],),
        )
    conn.commit()
    job = factory_job_factory(project["id"], spec="anything")

    brief = generate_brief(conn, job["id"], project["id"], job["spec"])

    assert rule_empty["id"] not in {r.id for r in brief.rules}
