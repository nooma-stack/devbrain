"""Tests for per-project dev ACL enforcement in FactoryDB.create_job.

Migration 028 adds devbrain.devs.allowed_projects TEXT[]:
  NULL  → all projects allowed (default; preserves existing behavior)
  []    → no projects allowed
  ['slug1'] → only that slug allowed
"""
from __future__ import annotations

import pytest
from state_machine import FactoryDB


@pytest.fixture
def db(database_url):
    return FactoryDB(database_url)


@pytest.fixture(autouse=True)
def cleanup(db):
    """Remove test devs before and after each test."""
    def _purge():
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM devbrain.devs WHERE dev_id LIKE 'test_acl_%%'"
            )
            conn.commit()
    _purge()
    yield
    _purge()


@pytest.mark.db
def test_null_allowed_projects_permits_any_project(db, project_factory):
    """Dev with allowed_projects=NULL can submit to any project."""
    project = project_factory("acl_open")
    dev_id = "test_acl_null"
    db.register_dev(dev_id, full_name="Null ACL Dev")
    # allowed_projects defaults to NULL; no explicit set needed

    # Should not raise
    job_id = db.create_job(
        project_slug=project["slug"],
        title="test job",
        spec="test",
        submitted_by=dev_id,
    )
    assert job_id


@pytest.mark.db
def test_empty_allowed_projects_blocks_all(db, project_factory):
    """Dev with allowed_projects=[] cannot submit to any project."""
    project = project_factory("acl_empty")
    dev_id = "test_acl_empty"
    db.register_dev(dev_id, full_name="Locked Out Dev")
    db.set_dev_allowed_projects(dev_id, [])

    with pytest.raises(PermissionError, match="not permitted"):
        db.create_job(
            project_slug=project["slug"],
            title="blocked job",
            spec="test",
            submitted_by=dev_id,
        )


@pytest.mark.db
def test_specific_slug_allows_matching_project(db, project_factory):
    """Dev restricted to a slug can submit to that project but not others."""
    allowed_project = project_factory("brightbot")
    other_project = project_factory("lht_vps")
    dev_id = "test_acl_specific"
    db.register_dev(dev_id, full_name="Restricted Dev")
    db.set_dev_allowed_projects(dev_id, [allowed_project["slug"]])

    # Allowed project: should succeed
    job_id = db.create_job(
        project_slug=allowed_project["slug"],
        title="allowed job",
        spec="test",
        submitted_by=dev_id,
    )
    assert job_id

    # Other project: should raise
    with pytest.raises(PermissionError, match="not permitted"):
        db.create_job(
            project_slug=other_project["slug"],
            title="blocked job",
            spec="test",
            submitted_by=dev_id,
        )


@pytest.mark.db
def test_unknown_dev_submitted_by_skips_acl_check(db, project_factory):
    """If submitted_by is None or not a registered dev, the ACL check is skipped."""
    project = project_factory("acl_anon")

    # No submitted_by — should not raise
    job_id = db.create_job(
        project_slug=project["slug"],
        title="anon job",
        spec="test",
        submitted_by=None,
    )
    assert job_id


@pytest.mark.db
def test_set_dev_allowed_projects_persists(db):
    """set_dev_allowed_projects stores the value and get_dev returns it."""
    dev_id = "test_acl_persist"
    db.register_dev(dev_id, full_name="Persist Dev")
    db.set_dev_allowed_projects(dev_id, ["brightbot", "lht-vps"])

    dev = db.get_dev(dev_id)
    assert dev["allowed_projects"] == ["brightbot", "lht-vps"]


@pytest.mark.db
def test_set_dev_allowed_projects_null_restores_all_access(db, project_factory):
    """Setting allowed_projects back to None restores unrestricted access."""
    project = project_factory("acl_restore")
    dev_id = "test_acl_restore"
    db.register_dev(dev_id, full_name="Restore Dev")
    db.set_dev_allowed_projects(dev_id, [])  # lock out
    db.set_dev_allowed_projects(dev_id, None)  # restore

    # Should not raise
    job_id = db.create_job(
        project_slug=project["slug"],
        title="restored job",
        spec="test",
        submitted_by=dev_id,
    )
    assert job_id
