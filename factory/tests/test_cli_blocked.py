"""Tests for the blocked and resolve CLI commands."""
import pytest
from click.testing import CliRunner
from cli import cli
from state_machine import FactoryDB, JobStatus

from config import DATABASE_URL


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture(autouse=True)
def cleanup(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT id FROM devbrain.factory_jobs WHERE title LIKE '%cli_blocked_test_%'")
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            cur.execute("DELETE FROM devbrain.factory_cleanup_reports WHERE job_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM devbrain.factory_artifacts WHERE job_id = ANY(%s)", (ids,))
            cur.execute("DELETE FROM devbrain.factory_jobs WHERE id = ANY(%s)", (ids,))
        conn.commit()


@pytest.fixture
def runner():
    return CliRunner()


def test_blocked_command_lists_blocked_jobs(runner, db):
    job_id = db.create_job(project_slug="devbrain", title="cli_blocked_test_1", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.BLOCKED)

    result = runner.invoke(cli, ["blocked"])
    assert result.exit_code == 0
    assert "cli_blocked_test_1" in result.output


def test_blocked_command_filter_by_project(runner):
    result = runner.invoke(cli, ["blocked", "--project", "nonexistent_project_xyz"])
    assert result.exit_code == 0
    assert "No blocked jobs" in result.output


def test_resolve_proceed_sets_field(runner, db, monkeypatch):
    import subprocess as _sub
    monkeypatch.setattr(_sub, "Popen", lambda *a, **kw: None)

    job_id = db.create_job(project_slug="devbrain", title="cli_blocked_test_2", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.BLOCKED)

    result = runner.invoke(cli, ["resolve", job_id[:8], "--proceed"])
    assert result.exit_code == 0, result.output
    assert "proceed" in result.output.lower()

    job = db.get_job(job_id)
    assert job.blocked_resolution == "proceed"


def test_resolve_replan_sets_field(runner, db, monkeypatch):
    import subprocess as _sub
    monkeypatch.setattr(_sub, "Popen", lambda *a, **kw: None)

    job_id = db.create_job(project_slug="devbrain", title="cli_blocked_test_3", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.BLOCKED)

    result = runner.invoke(cli, ["resolve", job_id[:8], "--replan"])
    assert result.exit_code == 0
    job = db.get_job(job_id)
    assert job.blocked_resolution == "replan"


def test_resolve_cancel_sets_field(runner, db, monkeypatch):
    import subprocess as _sub
    monkeypatch.setattr(_sub, "Popen", lambda *a, **kw: None)

    job_id = db.create_job(project_slug="devbrain", title="cli_blocked_test_4", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.BLOCKED)

    result = runner.invoke(cli, ["resolve", job_id[:8], "--cancel"])
    assert result.exit_code == 0
    job = db.get_job(job_id)
    assert job.blocked_resolution == "cancel"


def test_resolve_requires_action_flag(runner, db):
    job_id = db.create_job(project_slug="devbrain", title="cli_blocked_test_5", spec="Test")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.BLOCKED)
    result = runner.invoke(cli, ["resolve", job_id[:8]])
    assert result.exit_code != 0


def test_resolve_nonexistent_job(runner):
    result = runner.invoke(cli, ["resolve", "nonexist", "--proceed"])
    assert result.exit_code != 0
