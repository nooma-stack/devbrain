"""Injection screening for the cognify passes (see ingest/screening.py).

Loads the rules module from ingest/ by path (cognify runs with factory/ on
sys.path, ingest with ingest/), adds the config switch and the stored-flag
column check, and the helpers the passes call before text reaches a model.

yaml ``ingest.screening.mode`` (env ``DEVBRAIN_SCREENING``): on (default) |
off. "on" marks flagged chunks at ingest (memory.screen_flags, migration
049) and withholds flagged segments from summaries, extraction, fan-out and
re-summaries.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_PATH = Path(__file__).resolve().parents[2] / "ingest" / "screening.py"


def _load():
    name = "devbrain_ingest_screening"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


rules = _load()


def enabled() -> bool:
    env = os.environ.get("DEVBRAIN_SCREENING")
    if env is not None:
        return env.strip().lower() not in ("off", "0", "false", "no")
    try:
        from config import load_config as _load_cfg  # factory/config.py

        mode = ((_load_cfg().get("ingest") or {}).get("screening") or {}).get(
            "mode", "on"
        )
    except Exception:  # noqa: BLE001 — default on: screening only withholds
        mode = "on"
    if mode is False:
        return False
    return str(mode).strip().lower() not in ("off", "0", "false", "no")


_COLUMN: dict[int, bool] = {}


def has_flag_column(conn: Any) -> bool:
    """Whether memory.screen_flags exists (migration 049), cached per
    connection. Code may be deployed before the migration is applied."""
    key = id(conn)
    if key not in _COLUMN:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = 'devbrain' AND table_name = 'memory' "
                    "  AND column_name = 'screen_flags'"
                )
                _COLUMN[key] = cur.fetchone() is not None
        except Exception:  # noqa: BLE001
            _COLUMN[key] = False
    return _COLUMN[key]


def withhold(text: str, stored_flags: list[str] | None = None) -> tuple[str, list[str]]:
    """Text safe to put in a model prompt, and the flags that applied.

    Rules run on the fly, so rows ingested before screening existed are
    covered too. A row whose stored flags the rules no longer reproduce
    (e.g. rules changed) is withheld whole."""
    if not enabled():
        return text, []
    out, flags = rules.redact(text)
    if stored_flags and not flags:
        return (
            "[chunk withheld by injection screening: " + ",".join(stored_flags) + "]",
            list(stored_flags),
        )
    return out, flags


def withhold_usf(raw: str) -> tuple[str, list[str]]:
    if not enabled():
        return raw, []
    return rules.redact_usf_json(raw)
