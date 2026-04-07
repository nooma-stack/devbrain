"""CLI executor for the dev factory.

Spawns AI CLI tools (claude, codex, gemini) as subprocesses, each running
under its own subscription. The factory orchestrator calls these to
execute planning, implementation, and review phases.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class CLIResult:
    cli: str
    exit_code: int
    stdout: str
    stderr: str
    success: bool


# CLI tool configurations
CLI_CONFIGS = {
    "claude": {
        "command": "claude",
        "flag": "-p",  # prompt flag
        "extra_args": ["--output-format", "text", "--max-turns", "50"],
        "timeout": 600,  # 10 min
    },
    "codex": {
        "command": "codex",
        "flag": "--prompt",
        "extra_args": ["--auto"],
        "timeout": 600,
    },
    "gemini": {
        "command": "gemini",
        "flag": "-p",
        "extra_args": [],
        "timeout": 600,
    },
}

# Default CLI assignments per phase
DEFAULT_CLI_ASSIGNMENTS = {
    "planning": "claude",       # Best for reasoning/planning
    "implementing": "claude",   # Configurable per job
    "review_arch": "claude",    # Architecture review
    "review_security": "claude",  # Security/HIPAA review
    "fix": "claude",            # Fix loop
}


def is_cli_available(cli_name: str) -> bool:
    """Check if a CLI tool is installed and available."""
    config = CLI_CONFIGS.get(cli_name)
    if not config:
        return False
    return shutil.which(config["command"]) is not None


def get_available_clis() -> list[str]:
    """Return list of available CLI tools."""
    return [name for name in CLI_CONFIGS if is_cli_available(name)]


def run_cli(
    cli_name: str,
    prompt: str,
    cwd: str | None = None,
    timeout: int | None = None,
    env_override: dict | None = None,
) -> CLIResult:
    """Run a CLI tool with a prompt and return the result.

    The CLI runs under its own subscription — no API keys needed.
    """
    config = CLI_CONFIGS.get(cli_name)
    if not config:
        return CLIResult(
            cli=cli_name, exit_code=1,
            stdout="", stderr=f"Unknown CLI: {cli_name}",
            success=False,
        )

    if not is_cli_available(cli_name):
        return CLIResult(
            cli=cli_name, exit_code=1,
            stdout="", stderr=f"CLI not found: {config['command']}",
            success=False,
        )

    cmd = [config["command"], config["flag"], prompt] + config["extra_args"]
    effective_timeout = timeout or config["timeout"]

    logger.info("Running %s (timeout=%ds, cwd=%s)", cli_name, effective_timeout, cwd)

    import os
    env = os.environ.copy()
    if env_override:
        env.update(env_override)

    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            env=env,
        )
        success = result.returncode == 0
        if not success:
            logger.warning("%s exited with code %d: %s", cli_name, result.returncode, result.stderr[:500])

        return CLIResult(
            cli=cli_name,
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            success=success,
        )
    except subprocess.TimeoutExpired:
        logger.error("%s timed out after %ds", cli_name, effective_timeout)
        return CLIResult(
            cli=cli_name, exit_code=-1,
            stdout="", stderr=f"Timeout after {effective_timeout}s",
            success=False,
        )
    except Exception as e:
        logger.error("%s failed: %s", cli_name, e)
        return CLIResult(
            cli=cli_name, exit_code=-1,
            stdout="", stderr=str(e),
            success=False,
        )


def notify_desktop(title: str, message: str) -> None:
    """Send a macOS desktop notification."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "{title}"'],
            capture_output=True, timeout=5,
        )
    except Exception as e:
        logger.debug("Desktop notification failed: %s", e)
