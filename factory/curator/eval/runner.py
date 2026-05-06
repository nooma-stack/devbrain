"""Sequential eval invocation with warm prompt cache.

run_evals() spawns one claude subprocess per agent. eval_security runs
first and instantiates the cache (project + spec + brief + plan + diff).
eval_test runs second against the same cache, eval_perf runs third —
both hit cached input at ~10% cost. The agents share an identical
user-message body (`_build_cached_context`) — only the system prompt
differs — so claude's prompt cache (5-min TTL) is reused on each
subsequent call.

run_static_evals() runs eval_lint (subprocess-driven, zero LLM cost)
independently of the LLM chain. run_all_evals() orchestrates both.

Findings are persisted to `devbrain.factory_artifacts` (one row per
finding). The schema reuses the existing artifact columns:
- phase = 'reviewing'
- artifact_type = agent_name (e.g. 'eval_security', 'eval_test', ...)
- content = finding.model_dump_json()
- findings_count = 1
- metadata = {agent_name, version, elapsed_ms, started_at, error}

If an agent fails (subprocess exit non-zero, JSON parse error, timeout),
the EvalResult carries an empty findings list + the error string. The
OTHER agents still run — failure isolation is per-agent, not per-batch.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from curator.eval.types import EvalFinding, EvalResult

logger = logging.getLogger(__name__)

# Path to prompts dir — resolved relative to this module.
_PROMPTS_DIR = Path(__file__).parent / "prompts"

# Sequential ordering matters: eval_security primes the cache, eval_test
# and eval_perf hit it warm. Reversing this order doubles cost of
# subsequent calls. eval_perf is third (lowest cost-of-failure).
_LLM_AGENT_ORDER: list[tuple[str, str]] = [
    ("eval_security", "eval_security.md"),
    ("eval_test", "eval_test.md"),
    ("eval_perf", "eval_perf.md"),
]


def run_evals(
    conn: Any,
    job_id: UUID,
    brief: dict,
    plan: str,
    diff: str,
) -> list[EvalResult]:
    """Run LLM eval chain (security → test → perf), sharing a warm cache.

    Persists findings to factory_artifacts. Returns the EvalResult list
    in invocation order (security, test, perf).

    Failure isolation: if one agent raises, its EvalResult carries
    findings=[] + error=<exc>; the other agents still run.
    """
    cached_context = _build_cached_context(brief, plan, diff)
    results: list[EvalResult] = []

    for agent_name, prompt_file in _LLM_AGENT_ORDER:
        try:
            result = _invoke_agent(
                job_id=job_id,
                agent_name=agent_name,
                prompt_path=_PROMPTS_DIR / prompt_file,
                cached_context=cached_context,
            )
        except Exception as exc:  # noqa: BLE001 — agent failures must not abort the batch
            logger.exception("run_evals: %s failed", agent_name)
            result = EvalResult(
                version="1.0",
                job_id=job_id,
                agent_name=agent_name,
                findings=[],
                elapsed_ms=0,
                started_at=datetime.now(timezone.utc),
                error=str(exc)[:500],
            )
        results.append(result)

    _persist_findings(conn, job_id, results)
    return results


def _build_cached_context(brief: dict, plan: str, diff: str) -> str:
    """Format the shared eval context for prompt caching.

    Both eval agents (current and future) read the same context, so this
    is the cache-hit hot path. The exact byte-level shape of this string
    matters — any whitespace drift between the security and test calls
    breaks the cache. Don't reformat without intent.
    """
    return (
        f"## Brief\n\n{json.dumps(brief, indent=2)}\n\n"
        f"## Plan\n\n{plan}\n\n"
        f"## Diff\n\n```diff\n{diff}\n```\n"
    )


def _invoke_agent(
    job_id: UUID,
    agent_name: str,
    prompt_path: Path,
    cached_context: str,
) -> EvalResult:
    """Invoke claude with the eval prompt + cached context.

    First call instantiates cache; second call reuses it (claude's prompt
    cache TTL is 5 min, well within typical job phase duration).
    """
    started_at = datetime.now(timezone.utc)
    start = time.perf_counter()

    system_prompt = prompt_path.read_text()
    user_prompt = cached_context

    # Spawn claude — stdin payload, JSON output expected. The system
    # prompt differs per agent (eval_security.md vs eval_test.md); the
    # user message is identical so the cache is shared.
    proc = subprocess.run(
        [
            "claude",
            "-p",
            "--output-format", "json",
            "--system-prompt", system_prompt,
        ],
        input=user_prompt,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude exit {proc.returncode}: {proc.stderr[:500]}")

    response = json.loads(proc.stdout)
    findings = _parse_findings(agent_name, response)

    elapsed_ms = int((time.perf_counter() - start) * 1000)
    return EvalResult(
        version="1.0",
        job_id=job_id,
        agent_name=agent_name,
        findings=findings,
        elapsed_ms=elapsed_ms,
        started_at=started_at,
    )


def _parse_findings(agent_name: str, response: dict) -> list[EvalFinding]:
    """Delegate finding parsing to the per-agent module."""
    if agent_name == "eval_security":
        from curator.eval.eval_security import parse
    elif agent_name == "eval_test":
        from curator.eval.eval_test import parse
    elif agent_name == "eval_perf":
        from curator.eval.eval_perf import parse
    else:
        raise ValueError(f"unknown agent_name: {agent_name}")
    return parse(response)


def _persist_findings(conn: Any, job_id: UUID, results: list[EvalResult]) -> None:
    """Write findings to factory_artifacts (one row per finding).

    Maps onto the existing artifact schema:
    - phase = 'reviewing'
    - artifact_type = agent_name
    - content = finding.model_dump_json()
    - findings_count = 1
    - metadata = {version, agent_name, elapsed_ms, started_at, error}

    Each result also writes one summary row when findings is empty so
    the run is observable even on a clean diff (artifact_type =
    '<agent>_summary'). Errors are surfaced via metadata.error so the
    dashboard can render them.
    """
    with conn.cursor() as cur:
        for result in results:
            base_metadata = {
                "version": result.version,
                "agent_name": result.agent_name,
                "elapsed_ms": result.elapsed_ms,
                "started_at": result.started_at.isoformat(),
                "error": result.error,
            }
            if not result.findings:
                # Always write at least a summary row so run is observable.
                cur.execute(
                    "INSERT INTO devbrain.factory_artifacts "
                    "(job_id, phase, artifact_type, content, "
                    " findings_count, metadata) "
                    "VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
                    (
                        str(job_id),
                        "reviewing",
                        f"{result.agent_name}_summary",
                        "",
                        0,
                        json.dumps(base_metadata),
                    ),
                )
                continue
            for finding in result.findings:
                cur.execute(
                    "INSERT INTO devbrain.factory_artifacts "
                    "(job_id, phase, artifact_type, content, "
                    " findings_count, metadata) "
                    "VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
                    (
                        str(job_id),
                        "reviewing",
                        result.agent_name,
                        finding.model_dump_json(),
                        1,
                        json.dumps(base_metadata),
                    ),
                )
    conn.commit()


def _parse_diff_files(diff: str) -> list[str]:
    """Extract touched file paths from a unified diff string.

    Parses `+++ b/<path>` lines from a git-style unified diff. Returns
    a deduplicated list of file paths (without the 'b/' prefix). Falls
    back to an empty list if the diff is empty or unparseable.
    """
    import re
    files: list[str] = []
    seen: set[str] = set()
    for line in diff.splitlines():
        m = re.match(r"^\+\+\+ b/(.+)$", line)
        if m:
            path = m.group(1)
            if path not in seen:
                seen.add(path)
                files.append(path)
    return files


def run_static_evals(
    conn: Any,
    job_id: UUID,
    diff_files: list[str],
) -> list[EvalResult]:
    """Run eval_lint (subprocess-driven). Independent of LLM cache chain.

    eval_lint runs against the diff's touched files only and requires no
    LLM call. Results are persisted to factory_artifacts via the same
    _persist_findings path used by the LLM chain.

    Failure isolation: if eval_lint errors, its EvalResult carries
    findings=[] + error=<exc>; callers are unaffected.
    """
    from curator.eval import eval_lint

    try:
        result = eval_lint.run(conn, job_id, diff_files)
    except Exception as exc:  # noqa: BLE001
        logger.exception("run_static_evals: eval_lint failed")
        result = EvalResult(
            version="1.0",
            job_id=job_id,
            agent_name="eval_lint",
            findings=[],
            elapsed_ms=0,
            started_at=datetime.now(timezone.utc),
            error=str(exc)[:500],
        )

    _persist_findings(conn, job_id, [result])
    return [result]


def run_all_evals(
    conn: Any,
    job_id: UUID,
    brief: dict,
    plan: str,
    diff: str,
    diff_files: list[str],
) -> list[EvalResult]:
    """Top-level orchestrator. Calls both LLM chain and static chain.

    Returns combined results in eval_security, eval_test, eval_perf,
    eval_lint order.

    Failure isolation: a failure in one chain does not abort the other.
    """
    llm_results = run_evals(conn, job_id, brief, plan, diff)
    static_results = run_static_evals(conn, job_id, diff_files)
    return llm_results + static_results
