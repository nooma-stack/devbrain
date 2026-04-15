"""Tests for `devbrain doctor`.

Covers JSON shape, exit code on failure, and that env var overrides surface.
The doctor probes real services (Postgres, Ollama), so these tests assume
a working local install — same assumption the rest of the test suite makes.
"""

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEVBRAIN_BIN = REPO_ROOT / "bin" / "devbrain"


def _run(env_overrides: dict | None = None) -> tuple[int, str]:
    """Run `devbrain doctor --json` and return (exit_code, stdout)."""
    import os

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
