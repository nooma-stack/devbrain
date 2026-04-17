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
