"""eval_lint — subprocess wrapper around ruff (Python) and eslint (JS/TS).

Runs the project's own linter config against the diff's touched files
only. Zero LLM cost. Findings are mapped to EvalFinding for consistency
with the LLM eval chain.

Design decisions:
- Uses the project's own ruff/eslint config — does NOT impose DevBrain
  rules. Skip cleanly when no config is detectable (avoids false-positive
  flood from running linters with default rules).
- Scoped to diff_files only — avoids re-linting the whole project.
- Non-zero linter exit is only an error when it's an internal crash,
  not when it's lint findings (ruff/eslint exit 1 on findings but that
  is not an error condition here).
- Severity mapping: ruff/eslint 'error' -> critical, 'warning' ->
  important, everything else -> minor.

Skip conditions (return EvalResult with findings=[] and skipped reason):
- No detectable ruff/eslint config in the working directory.
- Linter binary not in PATH (subprocess FileNotFoundError).
- diff_files is empty (nothing to lint).
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from curator.eval.types import EvalFinding, EvalResult

logger = logging.getLogger(__name__)

# Severity mapping from ruff/eslint output to EvalFinding.severity.
# ruff uses numeric levels (1=warning, 2=error); eslint uses strings.
_ESLINT_SEV_MAP: dict[int, str] = {
    2: "critical",
    1: "important",
    0: "minor",
}

_RUFF_SEV_MAP: dict[str, str] = {
    "E": "critical",   # error-level rules
    "W": "important",  # warning-level rules
}


def _has_ruff_config(cwd: Path) -> bool:
    """Return True if the directory has a detectable ruff config."""
    if (cwd / "ruff.toml").exists():
        return True
    pyproject = cwd / "pyproject.toml"
    if pyproject.exists():
        try:
            content = pyproject.read_text()
            return "[tool.ruff]" in content
        except OSError:
            pass
    return False


def _has_eslint_config(cwd: Path) -> bool:
    """Return True if the directory has a detectable eslint config."""
    for pattern in (
        ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.yaml",
        ".eslintrc.yml", ".eslintrc.json", ".eslintrc",
        "eslint.config.js", "eslint.config.cjs", "eslint.config.mjs",
    ):
        if (cwd / pattern).exists():
            return True
    package_json = cwd / "package.json"
    if package_json.exists():
        try:
            data = json.loads(package_json.read_text())
            if "eslintConfig" in data:
                return True
            deps = {}
            deps.update(data.get("dependencies", {}))
            deps.update(data.get("devDependencies", {}))
            if any("eslint" in k for k in deps):
                return True
        except (OSError, json.JSONDecodeError):
            pass
    return False


def _ruff_severity(rule_code: str) -> str:
    """Map a ruff rule code prefix to EvalFinding severity.

    ruff uses letter prefixes: E = pycodestyle errors, W = warnings,
    F = pyflakes, etc. 'E' -> critical, 'W' -> important, rest -> minor.
    """
    if not rule_code:
        return "minor"
    return _RUFF_SEV_MAP.get(rule_code[0], "minor")


def _eslint_severity(level: int) -> str:
    """Map eslint severity int (0/1/2) to EvalFinding severity."""
    return _ESLINT_SEV_MAP.get(level, "minor")


def _run_ruff(diff_files: list[str], cwd: Path) -> list[EvalFinding]:
    """Run ruff on diff_files and parse JSON output to EvalFindings."""
    py_files = [f for f in diff_files if f.endswith(".py")]
    if not py_files:
        return []

    proc = subprocess.run(
        ["ruff", "check", "--output-format=json", *py_files],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )
    # ruff exits 0 (no findings), 1 (findings found), or 2 (internal error).
    if proc.returncode == 2:
        raise RuntimeError(f"ruff internal error: {proc.stderr[:500]}")

    if not proc.stdout.strip():
        return []

    findings: list[EvalFinding] = []
    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ruff output not valid JSON: {exc}") from exc

    for item in raw:
        rule_code = item.get("code") or ""
        severity = _ruff_severity(rule_code)
        fix_hint = ""
        if item.get("fix"):
            fix_hint = item["fix"].get("message", "")
        findings.append(
            EvalFinding(
                rule_id=None,
                severity=severity,  # type: ignore[arg-type]
                file=item.get("filename", ""),
                line=item.get("location", {}).get("row"),
                message=f"[{rule_code}] {item.get('message', '')}",
                fix_hint=fix_hint,
                relevant_memory_id=None,
            )
        )
    return findings


def _run_eslint(diff_files: list[str], cwd: Path) -> list[EvalFinding]:
    """Run eslint on diff_files and parse JSON output to EvalFindings."""
    js_files = [
        f for f in diff_files
        if any(f.endswith(ext) for ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"))
    ]
    if not js_files:
        return []

    proc = subprocess.run(
        ["npx", "--no", "eslint", "--format=json", *js_files],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )
    # eslint exits 0 (no findings), 1 (findings or lint warnings), 2 (error).
    if proc.returncode == 2:
        raise RuntimeError(f"eslint internal error: {proc.stderr[:500]}")

    if not proc.stdout.strip():
        return []

    findings: list[EvalFinding] = []
    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"eslint output not valid JSON: {exc}") from exc

    for file_result in raw:
        file_path = file_result.get("filePath", "")
        for msg in file_result.get("messages", []):
            rule_id_str = msg.get("ruleId") or ""
            severity = _eslint_severity(msg.get("severity", 1))
            findings.append(
                EvalFinding(
                    rule_id=None,
                    severity=severity,  # type: ignore[arg-type]
                    file=file_path,
                    line=msg.get("line"),
                    message=f"[{rule_id_str}] {msg.get('message', '')}",
                    fix_hint=msg.get("fix", {}).get("text", "") if msg.get("fix") else "",
                    relevant_memory_id=None,
                )
            )
    return findings


def run(
    conn: Any,
    job_id: UUID,
    diff_files: list[str],
    *,
    cwd: Path | None = None,
) -> EvalResult:
    """Run ruff and/or eslint on diff_files. Return an EvalResult.

    conn and job_id are accepted for interface symmetry with LLM agents
    but eval_lint does not write to the DB itself — the runner's
    _persist_findings handles that.

    cwd defaults to Path.cwd(). Passing an explicit cwd is used by tests.

    Skip conditions (returns EvalResult with findings=[] + skipped set):
    - diff_files is empty
    - No ruff config AND no eslint config detected
    - Linter binary not in PATH (FileNotFoundError)
    """
    started_at = datetime.now(timezone.utc)
    start = time.perf_counter()

    if cwd is None:
        cwd = Path.cwd()

    if not diff_files:
        return EvalResult(
            version="1.0",
            job_id=job_id,
            agent_name="eval_lint",
            findings=[],
            elapsed_ms=0,
            started_at=started_at,
            skipped="no diff files",
        )

    has_ruff = _has_ruff_config(cwd)
    has_eslint = _has_eslint_config(cwd)

    if not has_ruff and not has_eslint:
        return EvalResult(
            version="1.0",
            job_id=job_id,
            agent_name="eval_lint",
            findings=[],
            elapsed_ms=0,
            started_at=started_at,
            skipped="no lint config detected",
        )

    findings: list[EvalFinding] = []
    errors: list[str] = []

    if has_ruff:
        if shutil.which("ruff") is None:
            return EvalResult(
                version="1.0",
                job_id=job_id,
                agent_name="eval_lint",
                findings=[],
                elapsed_ms=0,
                started_at=started_at,
                skipped="ruff not in PATH",
            )
        try:
            findings.extend(_run_ruff(diff_files, cwd))
        except Exception as exc:  # noqa: BLE001
            logger.warning("eval_lint: ruff failed: %s", exc)
            errors.append(f"ruff: {exc!s}"[:200])

    if has_eslint:
        try:
            findings.extend(_run_eslint(diff_files, cwd))
        except FileNotFoundError:
            logger.warning("eval_lint: npx not in PATH, skipping eslint")
            errors.append("eslint: npx not in PATH")
        except Exception as exc:  # noqa: BLE001
            logger.warning("eval_lint: eslint failed: %s", exc)
            errors.append(f"eslint: {exc!s}"[:200])

    elapsed_ms = int((time.perf_counter() - start) * 1000)
    error_str = "; ".join(errors) if errors else None

    return EvalResult(
        version="1.0",
        job_id=job_id,
        agent_name="eval_lint",
        findings=findings,
        elapsed_ms=elapsed_ms,
        started_at=started_at,
        error=error_str,
    )
