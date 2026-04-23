"""Tests that reviewer prompts (arch + security) carry calibrated
severity guidance after PR #29 made WARNING findings trigger a fix
round. Captures the prompts via a fake run_cli (option (c) from the
2b-prime spec — the prompts are built inline in _run_review, so this
is the least invasive way to read them)."""
import pytest

import orchestrator as orch_mod
from orchestrator import FactoryOrchestrator
from state_machine import FactoryDB, JobStatus
from config import DATABASE_URL

TEST_TITLE_PREFIX = "review_prompt_calibration_test_"


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
                "DELETE FROM devbrain.factory_artifacts "
                "WHERE job_id = ANY(%s)",
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


class _FakeCLIResult:
    """Shape-compatible with cli_executor.CLIResult."""
    def __init__(self, stdout: str = ""):
        self.cli = "claude"
        self.exit_code = 0
        self.stdout = stdout
        self.stderr = ""
        self.success = True


def _make_reviewing_job(db: FactoryDB, title: str):
    job_id = db.create_job(project_slug="devbrain", title=title, spec="test")
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.IMPLEMENTING)
    return db.get_job(job_id)


def _capture_prompts(monkeypatch):
    """Stub run_cli + subprocess.run. The returned list is appended
    to on every run_cli call — captured[0] is arch_prompt, captured[1]
    is sec_prompt."""
    captured = []

    def fake_run_cli(cli, prompt, *args, **kwargs):
        captured.append(prompt)
        return _FakeCLIResult(stdout="(no findings)")

    monkeypatch.setattr(orch_mod, "run_cli", fake_run_cli)
    monkeypatch.setattr(
        orch_mod.subprocess, "run",
        lambda *a, **k: _FakeCompleted(stdout="diff --git a/x b/x\n"),
    )
    return captured


def test_arch_prompt_contains_severity_cost_guidance(orch, db, monkeypatch):
    captured = _capture_prompts(monkeypatch)
    job = _make_reviewing_job(db, f"{TEST_TITLE_PREFIX}arch")

    orch._run_review(job)

    assert len(captured) >= 1, "run_cli was not called for arch review"
    arch_prompt = captured[0]
    assert "Severity drives behavior" in arch_prompt
    assert "fix-loop iterates" in arch_prompt
    assert "each flag costs one implementer round" in arch_prompt
    assert "Err toward NIT" in arch_prompt
    assert "RESOLVED vs still BLOCKING" in arch_prompt


def test_security_prompt_contains_severity_cost_guidance(orch, db, monkeypatch):
    captured = _capture_prompts(monkeypatch)
    job = _make_reviewing_job(db, f"{TEST_TITLE_PREFIX}sec")

    orch._run_review(job)

    assert len(captured) >= 2, "run_cli was not called for security review"
    sec_prompt = captured[1]
    assert "Severity drives behavior" in sec_prompt
    assert "fix-loop iterates" in sec_prompt
    assert "each flag costs one implementer round" in sec_prompt
    assert "Err toward NIT" in sec_prompt
    assert "defense-in-depth" in sec_prompt
    assert "RESOLVED vs still BLOCKING" in sec_prompt
