"""Tests for FactoryOrchestrator._setup_implementation_branch.

Covers the four documented branch-resolution paths:
    1. branch_name unset → auto-create factory/<id>/<slug> (regression)
    2. branch_name set, branch exists → checkout + return name
    3. branch_name set, branch missing → warn + fall back to auto-create
    4. branch_name in {main, master} (case-insensitive) → return fail_msg

The helper invokes ``orchestrator.subprocess.run`` directly, so we
monkeypatch it on the imported module to avoid touching git.
"""
import logging

import pytest

import orchestrator as orchestrator_module
from config import DATABASE_URL
from orchestrator import FactoryOrchestrator
from state_machine import FactoryDB

TEST_TITLE_PREFIX = "fbranch_setup_"


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
                "DELETE FROM devbrain.factory_jobs WHERE id = ANY(%s)",
                (ids,),
            )
        conn.commit()


@pytest.fixture
def orch():
    return FactoryOrchestrator(DATABASE_URL)


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_job(db: FactoryDB, title: str, branch_name: str | None = None):
    job_id = db.create_job(project_slug="devbrain", title=title, spec="test")
    if branch_name is not None:
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE devbrain.factory_jobs SET branch_name = %s WHERE id = %s",
                (branch_name, job_id),
            )
            conn.commit()
    return db.get_job(job_id)


def test_no_branch_set_auto_creates_factory_branch(orch, db, monkeypatch):
    """Regression: branch_name unset → auto-create factory/<id>/<slug>."""
    job = _make_job(db, f"{TEST_TITLE_PREFIX}auto_create")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(orchestrator_module.subprocess, "run", fake_run)

    branch, fail_msg = orch._setup_implementation_branch(job, "/tmp")

    assert fail_msg is None
    assert branch is not None
    assert branch.startswith(f"factory/{job.id[:8]}/")
    assert calls == [["git", "checkout", "-b", branch]]


def test_existing_branch_is_checked_out(orch, db, monkeypatch):
    """branch_name set + branch exists → checkout it, return its name."""
    job = _make_job(
        db, f"{TEST_TITLE_PREFIX}existing", branch_name="feature/existing-x"
    )
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        # checkout succeeds; status reports clean working tree
        return _FakeCompleted(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(orchestrator_module.subprocess, "run", fake_run)

    branch, fail_msg = orch._setup_implementation_branch(job, "/tmp")

    assert fail_msg is None
    assert branch == "feature/existing-x"
    # First invocation checks out the requested branch (no -b).
    assert calls[0] == ["git", "checkout", "feature/existing-x"]
    # No auto-create fallback should have fired.
    assert not any("-b" in c for c in calls)


def test_missing_branch_falls_back_with_warning(orch, db, monkeypatch, caplog):
    """branch_name set + branch missing → warn, fall back to auto-create."""
    job = _make_job(
        db, f"{TEST_TITLE_PREFIX}missing", branch_name="feature/does-not-exist"
    )
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["git", "checkout", "feature/does-not-exist"]:
            return _FakeCompleted(
                returncode=1,
                stderr=(
                    "error: pathspec 'feature/does-not-exist' did not match "
                    "any file(s) known to git"
                ),
            )
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(orchestrator_module.subprocess, "run", fake_run)

    with caplog.at_level(logging.WARNING, logger=orchestrator_module.__name__):
        branch, fail_msg = orch._setup_implementation_branch(job, "/tmp")

    assert fail_msg is None
    assert branch is not None
    assert branch.startswith(f"factory/{job.id[:8]}/")
    # Both the failed checkout and the auto-create should have happened.
    assert ["git", "checkout", "feature/does-not-exist"] in calls
    assert any(c[:3] == ["git", "checkout", "-b"] for c in calls)
    assert any("does not exist" in m for m in caplog.messages)


def test_main_branch_is_refused(orch, db, monkeypatch):
    """branch_name = 'main' → return fail_msg, do not invoke git."""
    job = _make_job(db, f"{TEST_TITLE_PREFIX}main", branch_name="main")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(orchestrator_module.subprocess, "run", fake_run)

    branch, fail_msg = orch._setup_implementation_branch(job, "/tmp")

    assert branch is None
    assert fail_msg is not None
    assert "main" in fail_msg
    assert "feature branches" in fail_msg
    assert calls == []


def test_master_branch_is_refused_case_insensitive(orch, db, monkeypatch):
    """branch_name = 'MASTER' → also refused; check happens case-insensitively."""
    job = _make_job(db, f"{TEST_TITLE_PREFIX}master", branch_name="MASTER")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(orchestrator_module.subprocess, "run", fake_run)

    branch, fail_msg = orch._setup_implementation_branch(job, "/tmp")

    assert branch is None
    assert fail_msg is not None
    assert "MASTER" in fail_msg
    assert calls == []


# ─── Injection-shape guards ────────────────────────────────────────────────
# The following cases cover branch names that would be parsed by git as
# flags or refspecs if passed through without validation. Each must
# fail-closed BEFORE any subprocess call — the assertion on `calls == []`
# is the real security check.

@pytest.mark.parametrize(
    "name,reason",
    [
        ("--help", "leading '-' → git flag"),
        ("--receive-pack=echo", "leading '-' with = → RCE on push"),
        ("-feature", "single leading '-'"),
        ("feature:refs/heads/main", "refspec form → main bypass"),
        ("feat with space", "whitespace → shell metachar"),
        ("feat~1", "git refspec shorthand"),
        ("feat^", "git refspec shorthand"),
        ("..escape", "leading dots"),
        (".hidden", "leading dot"),
        ("feat\\back", "backslash"),
        ("feat[glob", "brackets"),
        ("feat*star", "glob"),
        ("   ", "whitespace only"),
        # Note: branch_name == "" falls through `if job.branch_name:` as
        # falsy, taking the auto-create path — that's the "no branch
        # provided" case, not an injection attempt. Empty is handled by
        # the existing no-branch test, not here.
    ],
)
def test_unsafe_branch_names_refused_without_running_git(
    orch, db, monkeypatch, name, reason
):
    """Every unsafe branch shape must fail validation BEFORE any
    subprocess call — the zod layer catches fresh submissions, this
    protects the orchestrator from direct-DB bypasses."""
    job = _make_job(
        db, f"{TEST_TITLE_PREFIX}unsafe_{hash(name) & 0xffffff:x}",
        branch_name=name,
    )
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _FakeCompleted(returncode=0)

    monkeypatch.setattr(orchestrator_module.subprocess, "run", fake_run)

    branch, fail_msg = orch._setup_implementation_branch(job, "/tmp")

    assert branch is None, f"expected refusal for {name!r} ({reason})"
    assert fail_msg is not None
    # Critical: no git invocation should have fired for any of these.
    assert calls == [], f"git was called with unsafe branch {name!r}: {calls}"


def test_safe_branch_names_pass_validation(orch, db, monkeypatch):
    """Sanity check: ordinary branch names still work post-hardening."""
    safe_names = [
        "feature/my-work",
        "factory/abc12345/some-title",
        "user_fix_123",
        "release-1.2.3",
        "fix/ABC-42",
    ]
    for name in safe_names:
        job = _make_job(
            db, f"{TEST_TITLE_PREFIX}safe_{hash(name) & 0xffffff:x}",
            branch_name=name,
        )
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return _FakeCompleted(returncode=0)

        monkeypatch.setattr(orchestrator_module.subprocess, "run", fake_run)

        branch, fail_msg = orch._setup_implementation_branch(job, "/tmp")
        assert fail_msg is None, f"rejected valid name {name!r}: {fail_msg}"
        assert branch == name
        assert calls and calls[0][:2] == ["git", "checkout"]
