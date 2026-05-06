"""P_eval_lint_diff_scope — eval_lint only runs against diff files.

POSTULATE
---------
eval_lint only passes the diff's touched files to ruff/eslint — it does
NOT re-lint the whole project. A lint violation that exists in a file NOT
in diff_files must NOT appear in the findings.

This matters because the factory job only changed a subset of the project.
Re-linting untouched files would produce noise unrelated to the current
change and would slow down eval on large projects.

STATUS
------
Activated in Atlas Step 9 — eval_lint subprocess wrapper.
"""
from __future__ import annotations

import shutil
import sys
import textwrap
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "factory"))

from curator.eval.eval_lint import run  # noqa: E402


pytestmark = pytest.mark.skipif(
    shutil.which("ruff") is None,
    reason="ruff not in PATH — diff-scope postulate requires ruff",
)


class _FakeConn:
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


def test_violation_in_non_diff_file_is_not_reported(tmp_path):
    """A ruff violation in a file NOT in diff_files is not reported.

    Setup:
    - bad.py: has an f-string-without-placeholders violation (F541)
    - good.py: clean Python file
    - diff_files = ["good.py"]  (only good.py was touched)

    Expected: findings == [] (bad.py is not in diff scope)
    """
    # Write minimal ruff config enabling F rules.
    (tmp_path / "ruff.toml").write_text(
        textwrap.dedent("""\
            [lint]
            select = ["F"]
        """)
    )

    # bad.py has a lint violation.
    (tmp_path / "bad.py").write_text('x = f"no placeholders"\n')

    # good.py is clean.
    (tmp_path / "good.py").write_text("x = 1\n")

    conn = _FakeConn()
    # Only good.py is in the diff.
    result = run(conn, uuid4(), diff_files=["good.py"], cwd=tmp_path)

    assert result.skipped is None, (
        f"Expected no skip, got: {result.skipped!r}"
    )
    assert result.findings == [], (
        f"Expected no findings (bad.py is not in diff_files), "
        f"got: {result.findings}"
    )


def test_violation_in_diff_file_is_reported(tmp_path):
    """A ruff violation in a file that IS in diff_files IS reported.

    Companion to the above — ensures the scope filter is one-sided (it
    excludes non-diff files but includes diff files that have violations).
    """
    (tmp_path / "ruff.toml").write_text(
        textwrap.dedent("""\
            [lint]
            select = ["F"]
        """)
    )

    # bad.py has a lint violation AND is in diff_files.
    (tmp_path / "bad.py").write_text('x = f"no placeholders"\n')

    conn = _FakeConn()
    result = run(conn, uuid4(), diff_files=["bad.py"], cwd=tmp_path)

    assert result.skipped is None
    assert len(result.findings) >= 1, (
        "Expected at least one finding for f-string-without-placeholders (F541), "
        f"got: {result.findings}"
    )
