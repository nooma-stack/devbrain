"""CLI executor for the dev factory.

Spawns AI CLI tools (claude, codex, gemini) as subprocesses, each running
under its own subscription. The factory orchestrator calls these to
execute planning, implementation, and review phases.

Per-phase CLI assignments are configured in config/devbrain.yaml under
factory.cli_preferences. Jobs can override with assigned_cli (applies to
all phases for that job).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Load factory CLI preferences from config (env > yaml > defaults precedence)
from config import (  # noqa: E402
    CLI_PREFERENCES as _CLI_PREFERENCES,
    FACTORY_PERMISSIONS_EXTRA_TOOLS as _EXTRA_TOOLS,
    FACTORY_PERMISSIONS_TIER as _TIER,
    FACTORY_TIER_2_SUBCATEGORIES as _SUBCATS,
)


@dataclass
class CLIResult:
    cli: str
    exit_code: int
    stdout: str
    stderr: str
    success: bool


# Tool allowlists for the claude CLI's --allowedTools flag, keyed by
# factory permissions tier. Tier 3 is handled separately via
# --dangerously-skip-permissions.
#
# Claude's --allowedTools accepts Tool(pattern) entries. Bash(cmd:*) means
# any Bash invocation whose command starts with "cmd". We enumerate common
# dev-loop commands rather than allow Bash(*) since the latter is
# effectively tier 3.
FACTORY_CLAUDE_ALLOWLIST_TIER_1 = [
    "Read", "Grep", "Glob",
    "Bash(git log:*)", "Bash(git diff:*)", "Bash(git status:*)",
    "Bash(git show:*)", "Bash(git blame:*)",
    "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)",
    "Bash(wc:*)", "Bash(find:*)", "Bash(which:*)", "Bash(pwd)",
    # DevBrain MCP — read-only queries are part of the factory's job
    "mcp__devbrain__deep_search",
    "mcp__devbrain__get_project_context",
    "mcp__devbrain__get_source_context",
    "mcp__devbrain__list_projects",
    "mcp__devbrain__factory_status",
    "mcp__devbrain__factory_file_locks",
]

# Tier 2 is built from Tier 1 plus a set of opt-in/opt-out subcategories.
# Users pick which subcategories to include via `devbrain setup
# factory-permissions`. Defaults are set in config._DEFAULTS and chosen
# to cover the 80% case (full dev loop) while leaving `git_push` off
# by default so a human reviews factory output before pushing.
FACTORY_TIER_2_SUBCATEGORY_TOOLS: dict[str, list[str]] = {
    "file_modification": ["Write", "Edit"],
    "git_commit": [
        "Bash(git add:*)", "Bash(git commit:*)", "Bash(git branch:*)",
        "Bash(git checkout:*)", "Bash(git reset:*)", "Bash(git stash:*)",
        "Bash(git rebase:*)", "Bash(git tag:*)", "Bash(git worktree:*)",
    ],
    "git_push": [
        "Bash(git push:*)", "Bash(git pull:*)", "Bash(git fetch:*)",
        "Bash(git merge:*)", "Bash(gh:*)",
    ],
    "python": [
        "Bash(pytest:*)", "Bash(python:*)", "Bash(python3:*)",
        "Bash(ruff:*)", "Bash(black:*)", "Bash(mypy:*)",
        "Bash(uv:*)", "Bash(pip:*)", "Bash(pip3:*)",
    ],
    "node_typescript": [
        "Bash(npm:*)", "Bash(node:*)", "Bash(tsc:*)", "Bash(yarn:*)",
        "Bash(jest:*)", "Bash(prettier:*)", "Bash(eslint:*)",
        "Bash(pnpm:*)", "Bash(npx:*)",
    ],
    "build_tools": [
        "Bash(make:*)", "Bash(cargo:*)", "Bash(go:*)",
    ],
    "filesystem_ops": [
        "Bash(mkdir:*)", "Bash(cp:*)", "Bash(mv:*)", "Bash(touch:*)",
    ],
    "devbrain_mcp_writes": [
        "mcp__devbrain__store",
        "mcp__devbrain__end_session",
        "mcp__devbrain__devbrain_notify",
    ],
}

# Default enablement per subcategory. git_push defaults off so the factory
# commits locally and the developer reviews the branch before pushing.
FACTORY_TIER_2_SUBCATEGORY_DEFAULTS: dict[str, bool] = {
    "file_modification": True,
    "git_commit": True,
    "git_push": False,
    "python": True,
    "node_typescript": True,
    "build_tools": True,
    "filesystem_ops": True,
    "devbrain_mcp_writes": True,
}


def _tier_2_allowlist(subcategories: dict[str, bool]) -> list[str]:
    """Compose tier-2 allowlist from enabled subcategories on top of tier 1."""
    allowlist = list(FACTORY_CLAUDE_ALLOWLIST_TIER_1)
    for name, tools in FACTORY_TIER_2_SUBCATEGORY_TOOLS.items():
        if subcategories.get(name, FACTORY_TIER_2_SUBCATEGORY_DEFAULTS[name]):
            allowlist.extend(tools)
    return allowlist


def _build_claude_extra_args(
    tier: int,
    extra_tools: list[str],
    subcategories: dict[str, bool] | None = None,
) -> list[str]:
    """Build the claude CLI extra_args list based on the factory tier.

    Tier 1 and 2 pass --allowedTools entries derived from the tier's
    allowlist (tier 2 composed from subcategory toggles) plus any
    user-provided extras. Tier 3 uses --dangerously-skip-permissions.

    NOTE: `--max-turns` is NOT included here — it's per-phase and gets
    appended at call time in `run_cli()` based on the phase kwarg. A
    single global cap was too tight for implementing (heavy edits + test
    iterations) and too loose for review phases (read-only audit).
    """
    base = ["--output-format", "text"]
    if tier >= 3:
        return base + ["--dangerously-skip-permissions"]
    if tier == 2:
        allowlist = _tier_2_allowlist(subcategories or _SUBCATS)
    else:  # tier 1
        allowlist = list(FACTORY_CLAUDE_ALLOWLIST_TIER_1)
    allowlist = allowlist + list(extra_tools)
    args = list(base)
    for tool in allowlist:
        args.extend(["--allowedTools", tool])
    return args


# CLI tool configurations — claude's extra_args are tier-driven.
CLI_CONFIGS = {
    "claude": {
        "command": "claude",
        "flag": "-p",
        "extra_args": _build_claude_extra_args(_TIER, _EXTRA_TOOLS, _SUBCATS),
        "timeout": None,
    },
    "codex": {
        "command": "codex",
        "flag": "--prompt",
        "extra_args": ["--auto"],
        "timeout": None,
    },
    "gemini": {
        "command": "gemini",
        "flag": "-p",
        "extra_args": [],
        "timeout": None,
    },
}

# CLI assignments per phase — loaded from config/devbrain.yaml, falling back to claude
DEFAULT_CLI_ASSIGNMENTS = {
    "planning": _CLI_PREFERENCES.get("planning", "claude"),
    "implementing": _CLI_PREFERENCES.get("implementing", "claude"),
    "review_arch": _CLI_PREFERENCES.get("review_arch", "claude"),
    "review_security": _CLI_PREFERENCES.get("review_security", "claude"),
    "fix": _CLI_PREFERENCES.get("fix", "claude"),
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
    env_override: dict | None = None,
    phase: str | None = None,
) -> CLIResult:
    """Run a CLI tool with a prompt and return the result.

    The CLI runs under its own subscription — no API keys needed.

    When `phase` is provided and the CLI is claude, `--max-turns <N>` is
    appended with the per-phase ceiling from config (see
    factory.config.get_max_turns_for_phase). Pass the same phase name
    used in cli_preferences (planning, implementing, review_arch,
    review_security, qa, fix). Omitting phase uses the tightest default.
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

    # Claude-only: append --max-turns based on the calling phase. Codex
    # and Gemini don't have a stable cross-version equivalent of this
    # flag, so we leave them as-is and rely on their own internal caps.
    if cli_name == "claude":
        from config import get_max_turns_for_phase  # local import avoids cycle at module load
        max_turns = get_max_turns_for_phase(phase)
        cmd += ["--max-turns", str(max_turns)]

    logger.info("Running %s (phase=%s, cwd=%s)", cli_name, phase or "default", cwd)

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
