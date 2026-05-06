"""Tests for the devbrain CLI."""
import os

import pytest
from click.testing import CliRunner
from cli import cli, parse_channel

# The DB-touching CLI tests call commands that build their own FactoryDB
# from config.DATABASE_URL (computed at import time). They work when
# DEVBRAIN_DATABASE_URL is set in the environment before pytest starts,
# which ensures config.py resolves the correct URL. Skip them when the
# env var is absent — the database_url fixture skips on DEVBRAIN_DB_PASSWORD,
# but that doesn't propagate into the CLI's own DB connection.
_DB_URL_ENV = os.getenv("DEVBRAIN_DATABASE_URL") or os.getenv("DEVBRAIN_DB_PASSWORD")
_skip_db = pytest.mark.skipif(
    not _DB_URL_ENV,
    reason="DEVBRAIN_DATABASE_URL (or DEVBRAIN_DB_PASSWORD) not set; CLI DB tests require DB",
)


@pytest.fixture
def runner():
    return CliRunner()


def test_parse_channel():
    result = parse_channel("tmux:alice")
    assert result == {"type": "tmux", "address": "alice"}

    result = parse_channel("smtp:alice@example.com")
    assert result == {"type": "smtp", "address": "alice@example.com"}

    # URL with colons (only splits on first colon)
    result = parse_channel("webhook_slack:https://hooks.slack.com/test")
    assert result == {"type": "webhook_slack", "address": "https://hooks.slack.com/test"}


def test_parse_channel_invalid():
    import click
    with pytest.raises(click.BadParameter):
        parse_channel("notvalid")


@_skip_db
def test_register_command(runner):
    result = runner.invoke(cli, [
        "register",
        "--dev-id", "test_cli_reg",
        "--name", "CLI Test",
        "--channel", "tmux:test_cli_reg",
    ])
    assert result.exit_code == 0
    assert "registered" in result.output.lower()


@_skip_db
def test_register_multiple_channels(runner):
    result = runner.invoke(cli, [
        "register",
        "--dev-id", "test_cli_multi",
        "--channel", "tmux:test_cli_multi",
        "--channel", "smtp:test@example.com",
    ])
    assert result.exit_code == 0


@_skip_db
def test_history_command(runner):
    result = runner.invoke(cli, [
        "history", "--dev", "test_cli_reg", "--recent", "5",
    ])
    assert result.exit_code == 0


def test_history_nl_dry_run_handles_ollama(runner):
    """NL query either works (ollama running) or exits gracefully."""
    result = runner.invoke(cli, [
        "history", "--query", "failed jobs this week", "--dry-run",
    ])
    # Either 0 (ollama returned SQL) or 1 (ollama unreachable)
    assert result.exit_code in (0, 1)


@_skip_db
def test_status_command(runner):
    """devbrain status runs without error."""
    result = runner.invoke(cli, ["status"])
    assert result.exit_code == 0
    # Either shows "All quiet" or actual status
    assert (
        "quiet" in result.output.lower()
        or "active" in result.output.lower()
        or "completed" in result.output.lower()
        or "no active" in result.output.lower()
    )


@_skip_db
def test_status_with_project_filter(runner):
    """devbrain status --project works."""
    result = runner.invoke(cli, ["status", "--project", "devbrain"])
    assert result.exit_code == 0


def test_dashboard_command_exists(runner):
    """devbrain dashboard is a registered command."""
    result = runner.invoke(cli, ["dashboard", "--help"])
    assert result.exit_code == 0
    assert "dashboard" in result.output.lower()


def test_version_command(runner):
    """devbrain version runs without error and prints all four fields."""
    result = runner.invoke(cli, ["version"])
    assert result.exit_code == 0
    assert "commit:" in result.output
    assert "branch:" in result.output
    assert "working tree:" in result.output
    assert "DEVBRAIN_HOME:" in result.output
    # Guard against silent field drops: each label appears exactly once.
    for label in ("commit:", "branch:", "working tree:", "DEVBRAIN_HOME:"):
        assert result.output.count(label) == 1, f"{label} should appear once"


def test_version_help(runner):
    """devbrain version --help works."""
    result = runner.invoke(cli, ["version", "--help"])
    assert result.exit_code == 0
