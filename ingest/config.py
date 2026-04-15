"""DevBrain ingest configuration loader.

Loads from config/devbrain.yaml with environment variable overrides.
Precedence: env > yaml > built-in defaults.

Environment variables (all optional):
    DEVBRAIN_HOME            — DevBrain installation directory (default: inferred from this file)
    DEVBRAIN_CONFIG          — Path to config file (default: $DEVBRAIN_HOME/config/devbrain.yaml)
    DEVBRAIN_DATABASE_URL    — Full Postgres URL (overrides yaml.database.*)
    DEVBRAIN_OLLAMA_URL      — Ollama server URL (overrides yaml.embedding.url and summarization.url)
    DEVBRAIN_EMBEDDING_MODEL — Embedding model name (overrides yaml.embedding.model)
    DEVBRAIN_SUMMARY_MODEL   — Summarization model name (overrides yaml.summarization.model)
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

# Resolve DevBrain home (project root). Allow override via env for non-standard installs.
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
    "embedding": {
        "provider": "ollama",
        "url": "http://localhost:11434",
        "model": "snowflake-arctic-embed2",
        "dims": 1024,
    },
    "summarization": {
        "provider": "ollama",
        "url": "http://localhost:11434",
        "model": "qwen2.5:7b",
    },
    "chunking": {
        "max_tokens": 400,
        "overlap_tokens": 80,
    },
    "ingest": {
        "adapters": {},
        "project_mappings": {},
    },
    "factory": {},
    "notifications": {},
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Override wins on leaf conflicts."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> dict:
    """Load config with precedence: env > yaml > defaults."""
    cfg = _deep_merge({}, _DEFAULTS)

    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            yaml_cfg = yaml.safe_load(f) or {}
        cfg = _deep_merge(cfg, yaml_cfg)

    # Env var overrides for the most common fields
    if v := os.environ.get("DEVBRAIN_OLLAMA_URL"):
        cfg["embedding"]["url"] = v
        cfg["summarization"]["url"] = v
    if v := os.environ.get("DEVBRAIN_EMBEDDING_MODEL"):
        cfg["embedding"]["model"] = v
    if v := os.environ.get("DEVBRAIN_SUMMARY_MODEL"):
        cfg["summarization"]["model"] = v

    return cfg


def build_database_url(cfg: dict | None = None) -> str:
    """Construct a Postgres URL. Env var DEVBRAIN_DATABASE_URL wins if set."""
    if env_url := os.environ.get("DEVBRAIN_DATABASE_URL"):
        return env_url
    if cfg is None:
        cfg = load_config()
    db = cfg["database"]
    return (
        f"postgresql://{db['user']}:{db['password']}"
        f"@{db['host']}:{db['port']}/{db['database']}"
    )


_config = load_config()

DATABASE_URL = build_database_url(_config)
OLLAMA_URL = _config["embedding"]["url"]
EMBED_MODEL = _config["embedding"]["model"]
EMBED_DIMS = _config["embedding"]["dims"]
CHUNK_MAX_TOKENS = _config["chunking"]["max_tokens"]
CHUNK_OVERLAP_TOKENS = _config["chunking"]["overlap_tokens"]
SUMMARIZE_MODEL = _config["summarization"]["model"]
SUMMARIZE_URL = _config["summarization"]["url"]
ADAPTER_CONFIG = _config["ingest"].get("adapters", {})
PROJECT_MAPPINGS = _config["ingest"].get("project_mappings", {})
