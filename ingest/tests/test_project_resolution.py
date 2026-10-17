"""Project resolution precedence (no DB)."""

from __future__ import annotations

import pytest
from adapters import claude_code


@pytest.fixture
def cfg(monkeypatch):
    def set_(mappings, roots):
        monkeypatch.setattr(claude_code, "PROJECT_MAPPINGS", mappings)
        monkeypatch.setattr(claude_code, "AUTO_PROJECT_ROOTS", roots)

    return set_


def test_bare_root_mapping_does_not_swallow_root_folders(cfg):
    """A mapping for the bare workspace root (~/Trifekta -> trifekta-ai)
    applies to sessions started IN the root, while <root>/<folder> keeps
    the auto-root convention."""
    cfg({"/Users/u/Trifekta": "trifekta-ai"}, ["/Users/*/Trifekta"])
    assert claude_code._lookup_slug("/Users/u/Trifekta") == "trifekta-ai"
    assert claude_code._lookup_slug("/Users/u/Trifekta/devplane") == "devplane"
    assert claude_code._lookup_slug("/Users/u/Trifekta/devplane/sub") == "devplane"


def test_more_specific_mapping_still_beats_the_auto_root(cfg):
    cfg({"/Users/u/Trifekta/Trifekta/AI": "trifekta-ai"}, ["/Users/*/Trifekta"])
    assert claude_code._lookup_slug("/Users/u/Trifekta/Trifekta/AI/x") == "trifekta-ai"


def test_prefix_mapping_without_auto_root(cfg):
    cfg({"/Users/u/Nooma-Stack/Trifekta-AI": "trifekta-ai"}, [])
    assert (
        claude_code._lookup_slug("/Users/u/Nooma-Stack/Trifekta-AI/Leads/OrbitForm")
        == "trifekta-ai"
    )
    assert claude_code._lookup_slug("/Users/u/Nooma-Stack/Other") is None


def test_claude_code_decoded_project_dir(cfg, tmp_path):
    cfg({"/Users/u/Trifekta": "trifekta-ai"}, ["/Users/*/Trifekta"])
    adapter = claude_code.ClaudeCodeAdapter()
    p = tmp_path / "projects" / "-Users-u-Trifekta" / "s.jsonl"
    assert adapter.detect_project(p) == "trifekta-ai"
    p2 = tmp_path / "projects" / "-Users-u-Trifekta-devplane" / "s.jsonl"
    assert adapter.detect_project(p2) == "devplane"
