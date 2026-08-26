"""Tests for ingest/summarize.py's input window.

Guards the 2026-08-26 widening: the summarizer's input cut moved from a
hardcoded 12K chars ("for 7B model context") to the configurable
summarization.max_input_chars, and the request must carry an explicit
num_ctx — ollama's own default context (4K tokens on most models) would
otherwise silently re-truncate everything the wider window admits.
"""
from __future__ import annotations

import importlib
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_INGEST_DIR = str(Path(__file__).resolve().parent.parent.parent / "ingest")


@pytest.fixture()
def summarize():
    """Import ingest's summarize with a clean module cache.

    Both ingest/ and factory/ expose a top-level module literally named
    ``config``; whichever a prior test imported first stays cached and
    poisons ``from config import SUMMARIZE_MAX_INPUT_CHARS`` here. Evict
    both names, put ingest/ at the head of sys.path, import fresh, and
    restore afterwards so this test is order-proof in both directions.
    """
    saved = {m: sys.modules.pop(m, None) for m in ("config", "summarize")}
    sys.path.insert(0, _INGEST_DIR)
    try:
        yield importlib.import_module("summarize")
    finally:
        sys.path.remove(_INGEST_DIR)
        for name, mod in saved.items():
            if mod is not None:
                sys.modules[name] = mod
            else:
                sys.modules.pop(name, None)


def _run_summarize(summarize, text: str) -> dict:
    """Call summarize_text with urlopen captured; return the request body."""

    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data)
        captured["timeout"] = timeout
        return io.BytesIO(json.dumps({"response": "ok"}).encode())

    with patch.object(summarize.urllib.request, "urlopen", fake_urlopen):
        assert summarize.summarize_text(text) == "ok"
    return captured


def test_input_cut_uses_configured_window(summarize):
    limit = summarize.SUMMARIZE_MAX_INPUT_CHARS
    assert limit > 12000, "window must be wider than the old 7B-era 12K cut"

    captured = _run_summarize(summarize, "x" * (limit + 5000))
    prompt = captured["body"]["prompt"]
    # The transcript tail of the prompt carries exactly `limit` chars.
    assert prompt.endswith("x" * limit)
    assert not prompt.endswith("x" * (limit + 1))


def test_num_ctx_covers_the_window(summarize):
    captured = _run_summarize(summarize, "y" * 48000)
    opts = captured["body"]["options"]
    # 48K chars ≈ 16K tokens; num_ctx must admit input + response headroom,
    # and must be present at all (ollama's default would truncate to 4K).
    assert opts["num_ctx"] >= 48000 // 3
    assert opts["num_predict"] == 800


def test_timeout_is_configured_not_hardcoded(summarize):
    captured = _run_summarize(summarize, "z" * 1000)
    assert captured["timeout"] == summarize.SUMMARIZE_TIMEOUT_S
    assert summarize.SUMMARIZE_TIMEOUT_S >= 240, (
        "48K chars of prefill on a 27B-class local model runs minutes; "
        "the old 120s timeout would fail every long summary"
    )
