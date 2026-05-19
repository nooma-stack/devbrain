"""Tests for the `devbrain invite` CLI command (PR #146 open decision 1).

The command stages a dev + invitation + bootstrap key + kit in one
non-interactive call. Validation tests use click.testing.CliRunner; the
happy-path test uses a live DB and a temp ssh authorized_keys so the
side-effecting calls don't pollute the real ~/.ssh/.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

_FACTORY = Path(__file__).resolve().parents[1]
if str(_FACTORY) not in sys.path:
    sys.path.insert(0, str(_FACTORY))

import cli as cli_module


def test_invite_rejects_invalid_dev_id():
    runner = CliRunner()
    result = runner.invoke(
        cli_module.cli,
        [
            "invite", "BadDevId",
            "--full-name", "x", "--email", "x@y.z",
        ],
    )
    assert result.exit_code == 2
    assert "not a valid dev_id" in result.output


def test_invite_rejects_malformed_email():
    runner = CliRunner()
    result = runner.invoke(
        cli_module.cli,
        [
            "invite", "alice",
            "--full-name", "Alice", "--email", "not-an-email",
        ],
    )
    assert result.exit_code == 2
    assert "doesn't look like a valid email" in result.output


def test_invite_requires_full_name_and_email():
    runner = CliRunner()
    result = runner.invoke(cli_module.cli, ["invite", "alice"])
    # click should reject with usage error (exit 2)
    assert result.exit_code != 0
    # Either --full-name or --email is missing; both are required.
    assert "Missing option" in result.output or "Error" in result.output


def test_invite_help_lists_all_flags():
    runner = CliRunner()
    result = runner.invoke(cli_module.cli, ["invite", "--help"])
    assert result.exit_code == 0
    for flag in (
        "--full-name", "--email", "--cli", "--platform", "--agent-app",
        "--slack", "--notes", "--ttl-days", "--auto-activate", "--email-now",
    ):
        assert flag in result.output


@pytest.fixture
def isolated_authorized_keys(tmp_path, monkeypatch):
    """Redirect ~/.ssh/authorized_keys to a tmp file so the test's
    bootstrap-key append doesn't touch the real keyring."""
    fake_home = tmp_path / "fake-home"
    (fake_home / ".ssh").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: fake_home)
    yield fake_home / ".ssh" / "authorized_keys"


def test_invite_live_db_creates_invitation_and_kit(
    isolated_authorized_keys, tmp_path, monkeypatch,
):
    """End-to-end against the real DB. Skipped without DEVBRAIN_DB_PASSWORD."""
    if not os.environ.get("DEVBRAIN_DB_PASSWORD") and not os.environ.get("DEVBRAIN_TEST_DATABASE_URL"):
        pytest.skip("DB not configured for tests")

    # Avoid colliding with prior runs.
    dev_id = f"invite-test-{os.getpid()}"

    runner = CliRunner()
    result = runner.invoke(
        cli_module.cli,
        [
            "invite", dev_id,
            "--full-name", "Invite Test",
            "--email", f"{dev_id}@example.test",
            "--cli", "claude",
            "--platform", "auto",
            "--agent-app", "claude-cli",
            "--ttl-days", "1",
            "--no-auto-activate",
            "--no-email-now",
        ],
    )
    try:
        assert result.exit_code == 0, f"output:\n{result.output}"
        # Helper prints "Dev '<id>' staged." on success.
        assert f"Dev '{dev_id}' staged" in result.output
        assert "Onboarding kit:" in result.output
        # Bootstrap key landed in our isolated authorized_keys.
        ak = isolated_authorized_keys
        assert ak.exists()
        ak_content = ak.read_text()
        assert dev_id in ak_content
        assert "ssh-ed25519" in ak_content
        # No --email-now so SMTP shouldn't have been attempted; check the
        # output explicitly mentions the skip path.
        assert "Skipping auto-send" in result.output
    finally:
        # Best-effort cleanup of the test row + on-disk kit so reruns
        # don't accumulate state.
        from config import DATABASE_URL  # noqa: PLC0415
        from state_machine import FactoryDB  # noqa: PLC0415

        db = FactoryDB(DATABASE_URL)
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM devbrain.invitations WHERE dev_id = %s", (dev_id,),
            )
            cur.execute("DELETE FROM devbrain.devs WHERE dev_id = %s", (dev_id,))
            conn.commit()
