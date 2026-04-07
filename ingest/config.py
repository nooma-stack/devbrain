"""DevBrain ingest configuration loader."""

from __future__ import annotations

from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).parent.parent / "config" / "devbrain.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


_config = load_config()

DATABASE_URL = (
    f"postgresql://{_config['database']['user']}:{_config['database']['password']}"
    f"@{_config['database']['host']}:{_config['database']['port']}"
    f"/{_config['database']['database']}"
)
OLLAMA_URL = _config["embedding"]["url"]
EMBED_MODEL = _config["embedding"]["model"]
EMBED_DIMS = _config["embedding"]["dims"]
CHUNK_MAX_TOKENS = _config["chunking"]["max_tokens"]
CHUNK_OVERLAP_TOKENS = _config["chunking"]["overlap_tokens"]
SUMMARIZE_MODEL = _config["summarization"]["model"]
ADAPTER_CONFIG = _config["ingest"]["adapters"]
