"""Tests for per-job git worktree lifecycle in the factory.

Covers:
    1. _worktree_path_for_job derives the right path from job.id
    2. _get_job_cwd routes correctly:
       - no branch_name → project_root fallback
       - branch_name set + worktree missing → project_root fallback
       - branch_name set + worktree exists → worktree path
    3. _setup_implementation_branch creates a worktree with -b on a
       fresh job (new branch)
    4. _setup_implementation_branch returns fail_msg when worktree
       creation fails (non-zero git exit)
    5. cleanup_agent._cleanup_branch removes the worktree BEFORE
       deleting the branch (ordering matters — branch -D would
       reject if the worktree still holds it)
    6. cleanup_agent._cleanup_branch gracefully skips worktree
       removal when the worktree doesn't exist (pre-refactor jobs,
       planning-phase failures)
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import orchestrator as orchestrator_module
import cleanup_agent as cleanup_agent_module
from config import DATABASE_URL
from orchestrator import FactoryOrchestrator, _worktree_path_for_job
from cleanup_agent import CleanupAgent
from state_machine import FactoryDB

TEST_TITLE_PREFIX = "worktree_test_"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture(autouse=True)
def cleanup(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.factory_jobs WHERE title LIKE %s",
            (f"{TEST_TITLE_PREFIX}%",),
        )
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            cur.execute(
                "DELETE FROM devbrain.factory_cleanup_reports WHERE job_id = ANY(%s)",
                (ids,),
            )
            cur.execute(
                "DELETE FROM devbrain.factory_artifacts WHERE job_id = ANY(%s)",
                (ids,),
            )
            cur.execute(
                "UPDATE devbrain.factory_jobs SET blocked_by_job_id = NULL "
                "WHERE blocked_by_job_id = ANY(%s)",
                (ids,),
            )
            cur.execute(
                "DELETE FROM devbrain.factory_jobs WHERE id = ANY(%s)",
                (ids,),
            )
        conn.commit()


class _FakeCompleted:
    """Mimic subprocess.CompletedProcess just enough for our asserts."""
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_job(db, title, branch_name=None):
    job_id = db.create_job(project_slug="devbrain", title=title, spec="test")
    if branch_name is not None:
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE devbrain.factory_jobs SET branch_name = %s WHERE id = %s",
                (branch_name, job_id),
            )
            conn.commit()
    return db.get_job(job_id)


# ─── _worktree_path_for_job ────────────────────────────────────────────────


def test_worktree_path_derivation(db):
    """Path is always ~/devbrain-worktrees/<job.id>/ regardless of title."""
    job = _make_job(db, f"{TEST_TITLE_PREFIX}path_derivation")
    path = _worktree_path_for_job(job)
    expected = str(Path.home() / "devbrain-worktrees" / job.id)
    assert path == expected


# ─── _get_job_cwd routing ─────────────────────────────────────────────────


def test_get_job_cwd_falls_back_when_no_branch_name(db, tmp_path, monkeypatch):
    """Job without branch_name → returns project_root."""
    orch = FactoryOrchestrator(DATABASE_URL)
    job = _make_job(db, f"{TEST_TITLE_PREFIX}no_branch")
    monkeypatch.setattr(orch, "_get_project_root", lambda j: str(tmp_path))
    assert orch._get_job_cwd(job) == str(tmp_path)


def test_get_job_cwd_falls_back_when_worktree_missing(db, tmp_path, monkeypatch):
    """Job has branch_name but worktree dir doesn't exist → project_root."""
    orch = FactoryOrchestrator(DATABASE_URL)
    job = _make_job(
        db, f"{TEST_TITLE_PREFIX}missing_wt", branch_name="feature/whatever",
    )
    monkeypatch.setattr(orch, "_get_project_root", lambda j: str(tmp_path))
    # Random uuid → worktree almost certainly doesn't exist.
    expected_wt = _worktree_path_for_job(job)
    assert not Path(expected_wt).exists()
    assert orch._get_job_cwd(job) == str(tmp_path)


def test_get_job_cwd_returns_worktree_when_branch_and_dir_exist(
    db, tmp_path, monkeypatch,
):
    """Job has branch_name AND worktree dir exists → worktree path."""
    orch = FactoryOrchestrator(DATABASE_URL)
    job = _make_job(
        db, f"{TEST_TITLE_PREFIX}real_wt", branch_name="feature/exists",
    )
    # Redirect _worktree_path_for_job to a tmp dir that actually exists.
    fake_wt = tmp_path / "devbrain-worktrees" / job.id
    fake_wt.mkdir(parents=True)
    monkeypatch.setattr(
        orchestrator_module, "_worktree_path_for_job", lambda j: str(fake_wt),
    )
    monkeypatch.setattr(orch, "_get_project_root", lambda j: "/should/not/be/used")
    assert orch._get_job_cwd(job) == str(fake_wt)


# ─── _setup_implementation_branch creates a worktree ──────────────────────


def test_setup_branch_creates_worktree_with_b_on_fresh_job(db, monkeypatch):
    """No branch_name → worktree created with `-b <new-branch>`."""
    orch = FactoryOrchestrator(DATABASE_URL)
    job = _make_job(db, f"{TEST_TITLE_PREFIX}fresh_job")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(orchestrator_module.subprocess, "run", fake_run)

    branch, fail_msg = orch._setup_implementation_branch(job, "/tmp")

    assert fail_msg is None
    assert branch is not None
    assert branch.startswith(f"factory/{job.id[:8]}/")
    # Single call: `git worktree add <path> -b <branch>`.
    assert len(calls) == 1
    assert calls[0][:3] == ["git", "worktree", "add"]
    assert "-b" in calls[0]
    assert branch in calls[0]


def test_setup_branch_fails_job_on_worktree_creation_failure(db, monkeypatch):
    """Worktree creation returns non-zero → (None, fail_msg)."""
    orch = FactoryOrchestrator(DATABASE_URL)
    job = _make_job(db, f"{TEST_TITLE_PREFIX}wt_create_fail")

    def fake_run(cmd, **kwargs):
        return _FakeCompleted(
            returncode=1,
            stderr="fatal: '/some/path' already exists and is not an empty directory",
        )

    monkeypatch.setattr(orchestrator_module.subprocess, "run", fake_run)

    branch, fail_msg = orch._setup_implementation_branch(job, "/tmp")

    assert branch is None
    assert fail_msg is not None
    assert "worktree" in fail_msg.lower()


# ─── cleanup_agent removes worktree before branch ─────────────────────────


def test_cleanup_removes_worktree_before_branch(db, monkeypatch):
    """Cleanup issues `git worktree remove` then `git branch -D`, in order."""
    job = _make_job(
        db, f"{TEST_TITLE_PREFIX}cleanup_with_wt",
        branch_name="factory/abc12345/some-work",
    )
    # Real dir so the Path(worktree).exists() check passes.
    wt_path = _worktree_path_for_job(job)
    Path(wt_path).mkdir(parents=True, exist_ok=True)
    try:
        agent = CleanupAgent(db)
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return _FakeCompleted(returncode=0)

        monkeypatch.setattr(cleanup_agent_module.subprocess, "run", fake_run)
        monkeypatch.setattr(agent, "_get_project_root", lambda j: "/tmp")

        agent._cleanup_branch(job)

        worktree_idx = next(
            (i for i, c in enumerate(calls) if c[:3] == ["git", "worktree", "remove"]),
            -1,
        )
        branch_idx = next(
            (i for i, c in enumerate(calls) if c[:3] == ["git", "branch", "-D"]),
            -1,
        )
        assert worktree_idx >= 0, f"expected worktree remove, got: {calls}"
        assert branch_idx >= 0, f"expected branch -D, got: {calls}"
        assert worktree_idx < branch_idx, (
            "worktree remove must precede branch -D"
        )
    finally:
        if Path(wt_path).exists():
            import shutil as _sh
            _sh.rmtree(wt_path, ignore_errors=True)


def test_cleanup_skips_worktree_step_when_missing(db, monkeypatch):
    """No worktree dir → worktree remove NOT called; branch -D still runs."""
    job = _make_job(
        db, f"{TEST_TITLE_PREFIX}cleanup_no_wt",
        branch_name="factory/abc12345/no-worktree",
    )
    wt_path = _worktree_path_for_job(job)
    assert not Path(wt_path).exists()

    agent = CleanupAgent(db)
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(cleanup_agent_module.subprocess, "run", fake_run)
    monkeypatch.setattr(agent, "_get_project_root", lambda j: "/tmp")

    agent._cleanup_branch(job)

    assert not any(c[:3] == ["git", "worktree", "remove"] for c in calls), (
        f"should not invoke worktree remove when worktree missing; got {calls}"
    )
    assert any(c[:3] == ["git", "branch", "-D"] for c in calls), (
        f"branch -D still must run; got {calls}"
    )
