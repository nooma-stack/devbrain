"""Tests for `devbrain doctor`.

Covers JSON shape, exit code on failure, and that env var overrides surface.
The doctor probes real services (Postgres, Ollama), so these tests assume
a working local install — skipped when the devbrain .venv is not present
in the expected location (e.g. in worktrees that share the parent venv).
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEVBRAIN_BIN = REPO_ROOT / "bin" / "devbrain"
# The devbrain binary shells out via its own .venv; skip if that venv is
# absent (e.g. when running from a git worktree that shares the parent's
# .venv rather than having its own copy).
_VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
_DOCTOR_AVAILABLE = _VENV_PYTHON.exists()

pytestmark = pytest.mark.skipif(
    not _DOCTOR_AVAILABLE,
    reason="devbrain .venv not present at expected path — likely a worktree; run from main checkout",
)


def _run(env_overrides: dict | None = None) -> tuple[int, str]:
    """Run `devbrain doctor --json` and return (exit_code, stdout)."""
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    result = subprocess.run(
        [str(DEVBRAIN_BIN), "doctor", "--json"],
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, result.stdout


def test_doctor_emits_valid_json():
    _, stdout = _run()
    parsed = json.loads(stdout)
    assert isinstance(parsed, list)
    assert len(parsed) > 0
    for check in parsed:
        assert {"name", "status", "detail"} <= check.keys()
        assert check["status"] in ("PASS", "WARN", "FAIL")


def test_doctor_includes_expected_checks():
    _, stdout = _run()
    parsed = json.loads(stdout)
    names = {c["name"] for c in parsed}
    expected = {
        "devbrain_home",
        "config_file",
        "postgres_reachable",
        "pgvector_installed",
        "ollama_reachable",
        "mcp_server_built",
        "ingest_venv",
        "env_overrides",
    }
    assert expected <= names, f"missing checks: {expected - names}"


def test_doctor_fails_with_bad_database_url():
    code, stdout = _run({
        "DEVBRAIN_DATABASE_URL": "postgresql://nobody:nope@localhost:5433/nope",
    })
    assert code == 1, "doctor should exit 1 when Postgres is unreachable"
    parsed = json.loads(stdout)
    by_name = {c["name"]: c for c in parsed}
    assert by_name["postgres_reachable"]["status"] == "FAIL"


def test_doctor_reports_env_overrides():
    _, stdout = _run({"DEVBRAIN_DATABASE_URL": "postgresql://x:y@z:1/db"})
    parsed = json.loads(stdout)
    by_name = {c["name"]: c for c in parsed}
    assert "DEVBRAIN_DATABASE_URL" in by_name["env_overrides"]["detail"]
