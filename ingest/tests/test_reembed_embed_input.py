"""embed_input text-rule tests (no DB, no network).

Run from the ingest/ directory: ``cd ingest && python -m pytest tests/``
"""

from __future__ import annotations

from reembed_memory import embed_input


def test_store_rows_do_not_embed_the_title_twice():
    # The MCP store tool saves content as "title\n\ncontent".
    assert embed_input("decision", "Pick X", "Pick X\n\nBecause Y") == "Pick X\n\nBecause Y"


def test_titled_rows_without_the_prefix_get_title_and_content():
    assert embed_input("pattern", "Retry", "Back off on 429") == "Retry\nBack off on 429"


def test_untitled_and_summary_rows_embed_content_only():
    assert embed_input("note", None, "body") == "body"
    assert embed_input("session_summary", "t", "summary") == "summary"
    assert embed_input("chunk", "t", "chunk text") == "chunk text"
