"""P_eval_lint_skip_no_config — no config produces empty result + skip reason.

POSTULATE
---------
A project directory that has no ruff config (no ruff.toml, no
[tool.ruff] in pyproject.toml) AND no eslint config produces an
EvalResult with:
  - findings == []
  - skipped is not None (contains a human-readable reason)
  - error is None (this is a clean skip, not a failure)

This prevents false-positive floods from running linters with default
rules on projects that haven't opted into lint enforcement.

STATUS
------
Activated in Atlas Step 9 — eval_lint subprocess wrapper.
"""
from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "factory"))

from curator.eval.eval_lint import run  # noqa: E402


class _FakeConn:
    """Minimal stub — eval_lint does not write DB itself."""

    def cursor(self):
        return self

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *a, **kw):
        pass


def test_no_config_returns_skipped_not_error(tmp_path):
    """A directory with no lint config produces skipped, not error.

    The distinction matters: skipped = "we chose not to run",
    error = "we tried to run and something broke". The dashboard must
    render these differently (green skip badge vs red error badge).
    """
    # tmp_path is empty — no ruff.toml, no pyproject.toml, no .eslintrc*.
    conn = _FakeConn()
    result = run(conn, uuid4(), diff_files=["some_file.py"], cwd=tmp_path)

    assert result.findings == [], (
        "No findings expected when no lint config is present."
    )
    assert result.skipped is not None, (
        "EvalResult.skipped must be set when eval_lint skips due to missing config."
    )
    assert result.error is None, (
        "EvalResult.error must be None for a clean skip — "
        "error implies a failure, skip implies a deliberate no-op."
    )


def test_no_config_skipped_reason_is_descriptive(tmp_path):
    """The skipped reason string must be human-readable (not None or empty)."""
    conn = _FakeConn()
    result = run(conn, uuid4(), diff_files=["some_file.py"], cwd=tmp_path)

    assert result.skipped
    # The reason should mention config or detection to be useful in the dashboard.
    assert len(result.skipped) > 5, (
        f"skipped reason is too short to be useful: {result.skipped!r}"
    )
