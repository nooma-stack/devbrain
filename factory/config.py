"""DevBrain factory configuration loader.

Mirrors ingest/config.py — same yaml + env precedence, but exports the
constants the factory subsystem needs. The two loaders intentionally
duplicate ~30 lines of code rather than share via sys.path tricks.

See ingest/config.py for the canonical documentation of env vars.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

DEVBRAIN_HOME = Path(
    os.environ.get("DEVBRAIN_HOME", Path(__file__).resolve().parent.parent)
).resolve()
CONFIG_PATH = Path(
    os.environ.get("DEVBRAIN_CONFIG", DEVBRAIN_HOME / "config" / "devbrain.yaml")
)


_DEFAULTS: dict = {
    "database": {
        "host": "localhost",
        "port": 5433,
        "user": "devbrain",
        "password": "devbrain-local",
        "database": "devbrain",
    },
    "summarization": {
        "url": "http://localhost:11434",
        "model": "qwen2.5:7b",
    },
    "factory": {
        "max_concurrent_jobs": 2,
        "max_fix_loop_retries": 5,
        "fix_loop": {
            "warnings_trigger_retry": True,
        },
        "default_review_passes": ["architecture", "security_hipaa"],
        "cli_preferences": {},
        "cleanup": {
            "soft_timer_seconds": 600,
            "extension_seconds": 300,
            "hard_ceiling_seconds": 1800,
            "auto_archive_after_hours": 24,
            "branch_cleanup": True,
        },
        "project_paths": {},
        # Long-running processes that hold cached database credentials.
        # rotate-db-password reloads each one after the rotation lands
        # and verifies it can authenticate; if any verification fails
        # the rotation rolls back. See config/devbrain.yaml.example for
        # the schema of each entry.
        "cred_dependents": [
            {
                "id": "ingest_daemon",
                "type": "launchagent",
                "label": "com.devbrain.ingest",
                "plist": "~/Library/LaunchAgents/com.devbrain.ingest.plist",
                "verify": "tail_log_no_auth_errors",
                "verify_log": "~/devbrain/logs/ingest.err.log",
                "verify_window_seconds": 10,
            },
        ],
        # Permissions tier controls what factory-spawned claude subprocesses
        # are allowed to do. 1 = read-only audit, 2 = guarded dev (curated
        # allowlist), 3 = unrestricted (--dangerously-skip-permissions).
        # Default 3 preserves pre-tier behavior; fresh installs choose via
        # `devbrain setup mcp` and should land on 2.
        "permissions_tier": 3,
        "permissions_extra_allowed_tools": [],
        # Tier 2 sub-category toggles. git_push is default-off so the
        # developer reviews the factory's work before anything leaves the
        # local machine.
        "permissions_tier_2_subcategories": {
            "file_modification": True,
            "git_commit": True,
            "git_push": False,
            "python": True,
            "node_typescript": True,
            "build_tools": True,
            "filesystem_ops": True,
            "devbrain_mcp_writes": True,
        },
    },
    "notifications": {
        "notify_events": [],
        "channels": {},
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict:
    cfg = _deep_merge({}, _DEFAULTS)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            yaml_cfg = yaml.safe_load(f) or {}
        cfg = _deep_merge(cfg, yaml_cfg)
    if v := os.environ.get("DEVBRAIN_OLLAMA_URL"):
        cfg["summarization"]["url"] = v
    if v := os.environ.get("DEVBRAIN_SUMMARY_MODEL"):
        cfg["summarization"]["model"] = v
    return cfg


def build_database_url(cfg: dict | None = None) -> str:
    if env_url := os.environ.get("DEVBRAIN_DATABASE_URL"):
        return env_url
    if cfg is None:
        cfg = load_config()
    db = cfg["database"]
    return (
        f"postgresql://{db['user']}:{db['password']}"
        f"@{db['host']}:{db['port']}/{db['database']}"
    )


def project_path(project_slug: str) -> str | None:
    """Resolve a project's local checkout path from config.

    Returns None if not configured. Used by factory phases that need to
    operate on a project's source tree (e.g., running tests, committing code).
    """
    cfg = load_config()
    mappings = cfg.get("factory", {}).get("project_paths", {})
    raw = mappings.get(project_slug)
    if raw is None:
        return None
    return str(Path(raw).expanduser())


_config = load_config()

DATABASE_URL = build_database_url(_config)
OLLAMA_URL = _config["summarization"]["url"]
SUMMARIZE_MODEL = _config["summarization"]["model"]
NL_MODEL = _config["summarization"]["model"]  # alias used by cli.py NL history
FACTORY_CONFIG = _config.get("factory", {})
NOTIFICATIONS_CONFIG = _config.get("notifications", {})
CLEANUP_CONFIG = FACTORY_CONFIG.get("cleanup", {})
CLI_PREFERENCES = FACTORY_CONFIG.get("cli_preferences", {})
FACTORY_PERMISSIONS_TIER = int(FACTORY_CONFIG.get("permissions_tier", 3))
FACTORY_PERMISSIONS_EXTRA_TOOLS = list(
    FACTORY_CONFIG.get("permissions_extra_allowed_tools", [])
)
FACTORY_TIER_2_SUBCATEGORIES = dict(
    FACTORY_CONFIG.get("permissions_tier_2_subcategories", {})
)
FACTORY_CRED_DEPENDENTS = list(FACTORY_CONFIG.get("cred_dependents", []))

# Fix-loop trigger tier. When True (default as of 2026-04-23), reviewer
# WARNING findings also route a job through FIX_LOOP; when False the
# pre-2026-04-23 behavior (BLOCKING-only) is preserved.
_FIX_LOOP_CONFIG = FACTORY_CONFIG.get("fix_loop", {})
FACTORY_FIX_LOOP_WARNINGS_TRIGGER_RETRY = bool(
    _FIX_LOOP_CONFIG.get("warnings_trigger_retry", True)
)

# Per-phase --max-turns ceiling for claude subprocesses. Bumped to 200
# uniformly on 2026-04-25 after factory job f4fdab6a (P2.a unified memory
# table) failed at the 50-turn planning ceiling — the planner had spent
# turns on legitimate deep_search lookups that the previous, tighter
# limits couldn't accommodate. 200 across all phases gives every agent
# enough headroom to finish a real-sized job without being blindsided
# mid-thought; runaway protection is still in place via the wall-clock
# timeout in cli_executor. These defaults fire only on yaml-less
# installs — see config/devbrain.yaml.example for the documented knobs.
_FACTORY_MAX_TURNS_DEFAULTS = {
    "planning": 200,
    "implementing": 200,
    "review_arch": 200,
    "review_security": 200,
    "qa": 200,
    "fix": 200,
}
_FACTORY_MAX_TURNS = FACTORY_CONFIG.get("max_turns") or {}


def get_max_turns_for_phase(phase: str | None) -> int:
    """Return the --max-turns ceiling for a factory phase.

    Unknown phases fall back to the planning default (the tightest) so
    accidental misspellings in call sites fail-closed rather than quietly
    granting a high ceiling. Pass None for a safe generic default.
    """
    if not phase:
        return _FACTORY_MAX_TURNS_DEFAULTS["planning"]
    if phase in _FACTORY_MAX_TURNS:
        try:
            return int(_FACTORY_MAX_TURNS[phase])
        except (TypeError, ValueError):
            pass  # fall through to default for malformed yaml
    return _FACTORY_MAX_TURNS_DEFAULTS.get(phase, _FACTORY_MAX_TURNS_DEFAULTS["planning"])
