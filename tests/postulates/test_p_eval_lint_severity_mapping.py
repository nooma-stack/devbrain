"""P_eval_lint_severity_mapping — ruff severity mapping is correct.

POSTULATE
---------
eval_lint maps ruff rule code prefixes to EvalFinding.severity correctly:
  - E (pycodestyle error-level): "critical"
  - W (pycodestyle warning-level): "important"
  - All other prefixes (F, N, B, ANN, etc.): "minor"

The mapping is a deliberate business decision: ruff's 'E' rules are
errors in the pycodestyle sense (style issues that often mask bugs);
'W' rules are warnings; everything else is informational lint.

The eslint mapping is also validated:
  - severity=2 (error): "critical"
  - severity=1 (warning): "important"
  - severity=0 (off/info): "minor"

STATUS
------
Activated in Atlas Step 9 — eval_lint subprocess wrapper.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "factory"))

from curator.eval.eval_lint import _ruff_severity, _eslint_severity  # noqa: E402


# ---------------------------------------------------------------- ruff mapping

def test_ruff_e_prefix_maps_to_critical():
    """E-prefixed rules (pycodestyle errors) map to critical severity."""
    assert _ruff_severity("E501") == "critical"
    assert _ruff_severity("E711") == "critical"
    assert _ruff_severity("E999") == "critical"


def test_ruff_w_prefix_maps_to_important():
    """W-prefixed rules (pycodestyle warnings) map to important severity."""
    assert _ruff_severity("W291") == "important"
    assert _ruff_severity("W503") == "important"


def test_ruff_f_prefix_maps_to_minor():
    """F-prefixed rules (pyflakes) map to minor severity."""
    assert _ruff_severity("F401") == "minor"
    assert _ruff_severity("F541") == "minor"
    assert _ruff_severity("F811") == "minor"


def test_ruff_other_prefixes_map_to_minor():
    """All other ruff rule prefixes map to minor severity."""
    assert _ruff_severity("B006") == "minor"   # flake8-bugbear
    assert _ruff_severity("N803") == "minor"   # pep8-naming
    assert _ruff_severity("ANN001") == "minor"  # flake8-annotations
    assert _ruff_severity("UP007") == "minor"  # pyupgrade
    assert _ruff_severity("C901") == "minor"   # mccabe complexity


def test_ruff_empty_code_maps_to_minor():
    """Empty rule code (edge case) maps to minor, not an error."""
    assert _ruff_severity("") == "minor"


# ---------------------------------------------------------------- eslint mapping

def test_eslint_2_maps_to_critical():
    """eslint severity 2 (error) maps to critical."""
    assert _eslint_severity(2) == "critical"


def test_eslint_1_maps_to_important():
    """eslint severity 1 (warning) maps to important."""
    assert _eslint_severity(1) == "important"


def test_eslint_0_maps_to_minor():
    """eslint severity 0 (off) maps to minor."""
    assert _eslint_severity(0) == "minor"


def test_eslint_unknown_int_maps_to_minor():
    """Unknown eslint severity int falls back to minor (defensive)."""
    assert _eslint_severity(99) == "minor"
    assert _eslint_severity(-1) == "minor"
