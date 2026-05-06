"""Tests for eval_lint — subprocess wrapper around ruff + eslint.

Tests use a temporary directory with a synthetic ruff config and Python
files. No LLM is invoked. The ruff binary must be in PATH for the
ruff-specific tests; tests skip cleanly when it isn't.

Coverage gate: factory/curator/eval/eval_lint.py >= 85%.
"""
from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path
from uuid import uuid4

import pytest

from curator.eval.eval_lint import (
    _has_eslint_config,
    _has_ruff_config,
    _ruff_severity,
    _eslint_severity,
    run,
)
from curator.eval.types import EvalResult


# ---------------------------------------------------------------- helpers

def _job_id():
    return uuid4()


class _FakeConn:
    """Minimal psycopg2-style connection stub (eval_lint doesn't write DB)."""

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


# ---------------------------------------------------------------- unit tests

def test_ruff_severity_error_prefix():
    """Rule codes starting with 'E' map to critical."""
    assert _ruff_severity("E501") == "critical"
    assert _ruff_severity("E711") == "critical"


def test_ruff_severity_warning_prefix():
    """Rule codes starting with 'W' map to important."""
    assert _ruff_severity("W291") == "important"


def test_ruff_severity_other_prefix():
    """All other prefixes (F, N, B, etc.) map to minor."""
    assert _ruff_severity("F401") == "minor"
    assert _ruff_severity("B006") == "minor"
    assert _ruff_severity("N803") == "minor"


def test_ruff_severity_empty_code():
    """Empty rule code falls back to minor."""
    assert _ruff_severity("") == "minor"


def test_eslint_severity_levels():
    """eslint severity: 2 -> critical, 1 -> important, 0 -> minor."""
    assert _eslint_severity(2) == "critical"
    assert _eslint_severity(1) == "important"
    assert _eslint_severity(0) == "minor"


def test_eslint_severity_unknown_defaults_to_minor():
    """Unknown eslint severity int falls back to minor."""
    assert _eslint_severity(99) == "minor"


# ---------------------------------------------------------------- config detection

def test_has_ruff_config_ruff_toml(tmp_path):
    """ruff.toml in cwd is detected."""
    (tmp_path / "ruff.toml").write_text("[tool.ruff]\n")
    assert _has_ruff_config(tmp_path)


def test_has_ruff_config_pyproject_toml_with_tool_ruff(tmp_path):
    """pyproject.toml with [tool.ruff] section is detected."""
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 88\n")
    assert _has_ruff_config(tmp_path)


def test_has_ruff_config_pyproject_toml_without_tool_ruff(tmp_path):
    """pyproject.toml without [tool.ruff] is NOT detected as ruff config."""
    (tmp_path / "pyproject.toml").write_text("[tool.black]\nline-length = 88\n")
    assert not _has_ruff_config(tmp_path)


def test_has_ruff_config_absent(tmp_path):
    """No config files -> not detected."""
    assert not _has_ruff_config(tmp_path)


def test_has_eslint_config_eslintrc_json(tmp_path):
    """eslintrc.json is detected."""
    (tmp_path / ".eslintrc.json").write_text("{}")
    assert _has_eslint_config(tmp_path)


def test_has_eslint_config_package_json_with_eslint_dep(tmp_path):
    """package.json with eslint in devDependencies is detected."""
    (tmp_path / "package.json").write_text(
        json.dumps({"devDependencies": {"eslint": "^8.0.0"}})
    )
    assert _has_eslint_config(tmp_path)


def test_has_eslint_config_absent(tmp_path):
    """No eslint config -> not detected."""
    assert not _has_eslint_config(tmp_path)


# ---------------------------------------------------------------- run() skip conditions

def test_run_skips_when_no_diff_files():
    """Empty diff_files returns EvalResult with skipped set."""
    conn = _FakeConn()
    result = run(conn, _job_id(), diff_files=[])
    assert isinstance(result, EvalResult)
    assert result.findings == []
    assert result.skipped is not None
    assert "no diff files" in result.skipped


def test_run_skips_when_no_config(tmp_path):
    """Directory with no ruff/eslint config returns skipped result."""
    conn = _FakeConn()
    result = run(conn, _job_id(), diff_files=["foo.py"], cwd=tmp_path)
    assert result.findings == []
    assert result.skipped is not None
    assert "no lint config" in result.skipped


def test_run_skips_when_ruff_not_in_path(tmp_path, monkeypatch):
    """When ruff config exists but ruff is not in PATH, skip cleanly."""
    (tmp_path / "ruff.toml").write_text("[tool.ruff]\n")
    # Patch shutil.which to return None for ruff.
    monkeypatch.setattr(
        "curator.eval.eval_lint.shutil.which",
        lambda name: None if name == "ruff" else shutil.which(name),
    )
    conn = _FakeConn()
    result = run(conn, _job_id(), diff_files=["foo.py"], cwd=tmp_path)
    assert result.findings == []
    assert result.skipped is not None
    assert "ruff not in PATH" in result.skipped


# ---------------------------------------------------------------- ruff integration

@pytest.mark.skipif(
    shutil.which("ruff") is None,
    reason="ruff not in PATH — skipping real-ruff tests",
)
def test_ruff_finds_f_string_without_placeholders(tmp_path):
    """Synthetic Python file with f-string-without-placeholders (F541)
    produces a finding. F541 is the ruff rule for `f"no placeholders"`.
    """
    # Write a minimal ruff config (enables F rules).
    (tmp_path / "ruff.toml").write_text(
        textwrap.dedent("""\
            [lint]
            select = ["F"]
        """)
    )
    # Write a Python file with an f-string-without-placeholders violation.
    victim = tmp_path / "victim.py"
    victim.write_text('x = f"no placeholders here"\n')

    conn = _FakeConn()
    result = run(conn, _job_id(), diff_files=["victim.py"], cwd=tmp_path)

    assert isinstance(result, EvalResult)
    assert result.skipped is None
    assert len(result.findings) >= 1
    # The finding should reference the victim file and the F541 rule.
    f = result.findings[0]
    assert "victim.py" in f.file or "victim.py" in f.message or "F541" in f.message


@pytest.mark.skipif(
    shutil.which("ruff") is None,
    reason="ruff not in PATH — skipping real-ruff tests",
)
def test_ruff_clean_file_yields_empty_findings(tmp_path):
    """A syntactically clean Python file with no violations produces an
    empty findings list (not a skipped result)."""
    (tmp_path / "ruff.toml").write_text(
        textwrap.dedent("""\
            [lint]
            select = ["F", "E"]
        """)
    )
    clean = tmp_path / "clean.py"
    clean.write_text(
        textwrap.dedent("""\
            def add(a: int, b: int) -> int:
                return a + b
        """)
    )

    conn = _FakeConn()
    result = run(conn, _job_id(), diff_files=["clean.py"], cwd=tmp_path)

    assert result.skipped is None
    assert result.findings == []


@pytest.mark.skipif(
    shutil.which("ruff") is None,
    reason="ruff not in PATH — skipping real-ruff tests",
)
def test_ruff_only_lints_diff_files(tmp_path):
    """eval_lint only lints the files in diff_files, not the whole
    project — a violation in an unlisted file is NOT reported."""
    (tmp_path / "ruff.toml").write_text(
        textwrap.dedent("""\
            [lint]
            select = ["F"]
        """)
    )
    # File with violation — NOT in diff_files.
    bad = tmp_path / "bad.py"
    bad.write_text('x = f"no placeholders"\n')
    # File without violation — IN diff_files.
    good = tmp_path / "good.py"
    good.write_text("x = 1\n")

    conn = _FakeConn()
    result = run(conn, _job_id(), diff_files=["good.py"], cwd=tmp_path)

    # Should see no findings because bad.py was not in diff_files.
    assert result.findings == []
    assert result.skipped is None


# ---------------------------------------------------------------- EvalResult shape

def test_run_result_is_eval_result_type():
    """run() always returns an EvalResult instance."""
    conn = _FakeConn()
    result = run(conn, _job_id(), diff_files=[])
    assert isinstance(result, EvalResult)


def test_run_result_agent_name_is_eval_lint(tmp_path):
    """agent_name is always 'eval_lint'."""
    conn = _FakeConn()
    result = run(conn, _job_id(), diff_files=[], cwd=tmp_path)
    assert result.agent_name == "eval_lint"


def test_run_result_version_is_1_0(tmp_path):
    """version is always '1.0'."""
    conn = _FakeConn()
    result = run(conn, _job_id(), diff_files=[], cwd=tmp_path)
    assert result.version == "1.0"
