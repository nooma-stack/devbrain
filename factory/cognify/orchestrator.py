"""Cognify orchestrator — base class and pass registry.

The orchestrator provides:
  - A base class (CognifyPass) that every pass inherits from. Subclasses
    implement `run(conn, project_id, dry_run)` and the orchestrator wraps
    the call with cognify_run_log bookkeeping.
  - `run_pass(conn, pass_name, project_id, dry_run)` — the CLI-facing
    entrypoint. Dispatches to the registered pass class.
  - `run_all(conn, project_id, dry_run)` — runs all 5 passes in
    dependency order: decay → gc → extract → edges → strengthen.

Design invariant: cognify_run_log rows are project-scoped. If project_id
is None (applies to decay + gc which can sweep all projects), a row per
project is NOT created — a single project_id=NULL row is created instead
(the pass itself is cross-project). All per-project LLM passes (extract,
edges, strengthen) must pass an explicit project_id.

No raw PHI from memory.content flows into cognify_run_log rows.
"""
from __future__ import annotations

import json
import logging
import traceback
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ── Pass execution result ──────────────────────────────────────────────────────


@dataclass
class PassResult:
    """What a pass reports back to the orchestrator.

    rows_processed: count of memory rows touched (never raw content).
    llm_calls: how many LLM calls were made (0 for SQL-only passes).
    metadata: additional structured info (must NOT contain raw memory content).
    dry_run: if True, no DB mutations happened.
    """

    rows_processed: int = 0
    llm_calls: int = 0
    metadata: dict = None  # type: ignore[assignment]
    dry_run: bool = False

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


# ── Base class ──────────────────────────────────────────────────────────────


class CognifyPass(ABC):
    """Abstract base for all five cognify passes.

    Subclasses implement ``run()``; the orchestrator wraps the call with
    cognify_run_log bookkeeping.
    """

    #: Name registered with the orchestrator. Must match the CLI --pass value.
    pass_name: str = ""

    @abstractmethod
    def run(self, conn: Any, project_id: Any, *, dry_run: bool = False) -> PassResult:
        """Execute the pass.

        Args:
            conn: psycopg2 connection. The pass manages its own transactions.
            project_id: UUID of the project to operate on, or None for
                SQL-only passes that sweep all projects.
            dry_run: If True, compute what would happen but make no DB changes.

        Returns:
            PassResult with row/call counts.
        """
        ...


# ── Pass registry ──────────────────────────────────────────────────────────


# Populated lazily so we don't trigger circular imports at module load.
_PASS_REGISTRY: dict[str, type[CognifyPass]] = {}


def _ensure_registry() -> None:
    """Import all pass modules so they self-register."""
    if _PASS_REGISTRY:
        return
    # Import in dependency order (decay and gc first; extract before edges).
    from cognify import decay as _  # noqa: F401
    from cognify import gc as _  # noqa: F401
    from cognify import extract as _  # noqa: F401
    from cognify import edges as _  # noqa: F401
    from cognify import strengthen as _  # noqa: F401


def register_pass(cls: type[CognifyPass]) -> type[CognifyPass]:
    """Decorator: register a CognifyPass subclass under its pass_name."""
    assert cls.pass_name, f"{cls.__name__} must set pass_name"
    _PASS_REGISTRY[cls.pass_name] = cls
    return cls


# ── Dependency order ──────────────────────────────────────────────────────

PASS_ORDER = ["decay", "gc", "extract", "edges", "strengthen"]


# ── Main entrypoints ──────────────────────────────────────────────────────


def run_pass(
    conn: Any,
    pass_name: str,
    project_id: Any = None,
    *,
    dry_run: bool = False,
    cross_project: bool = False,
) -> PassResult:
    """Run a single named pass and record it in cognify_run_log.

    Args:
        conn: psycopg2 connection.
        pass_name: one of 'decay', 'gc', 'extract', 'edges', 'strengthen'.
        project_id: UUID or None.
        dry_run: if True, compute only; no DB mutations.
        cross_project: only honored by passes that opt in (currently
            'edges' for canonical-rule contradiction sweeps). Other
            passes ignore the flag.

    Returns:
        PassResult from the pass.

    Raises:
        ValueError: if pass_name is not registered.
    """
    _ensure_registry()
    if pass_name not in _PASS_REGISTRY:
        valid = sorted(_PASS_REGISTRY)
        raise ValueError(
            f"Unknown pass {pass_name!r}. Valid: {valid}"
        )

    pass_cls = _PASS_REGISTRY[pass_name]
    instance = pass_cls()

    log_id = _start_run_log(conn, pass_name, project_id)
    result = PassResult()
    error_text: str | None = None

    try:
        # Pass-specific kwargs: only forward cross_project to passes that
        # accept it. The introspection check keeps the signature optional
        # without forcing every pass to declare it.
        kwargs = {"dry_run": dry_run}
        import inspect
        sig = inspect.signature(instance.run)
        if "cross_project" in sig.parameters:
            kwargs["cross_project"] = cross_project
        result = instance.run(conn, project_id, **kwargs)
    except Exception as exc:
        error_text = traceback.format_exc()[:2000]
        logger.exception("cognify pass %s failed", pass_name)
    finally:
        _complete_run_log(conn, log_id, result, error_text, dry_run=dry_run)

    if error_text:
        raise RuntimeError(
            f"cognify pass {pass_name!r} failed — see cognify_run_log id={log_id}"
        ) from None

    return result


def run_all(
    conn: Any,
    project_id: Any = None,
    *,
    dry_run: bool = False,
    cross_project: bool = False,
) -> dict[str, PassResult]:
    """Run all 5 passes in dependency order.

    Returns a dict of {pass_name: PassResult}.
    Continues past individual pass failures (logged but not re-raised) so a
    flaky LLM in 'extract' doesn't block 'strengthen'.

    cross_project is forwarded to passes that accept it (currently 'edges').
    """
    _ensure_registry()
    results: dict[str, PassResult] = {}
    for name in PASS_ORDER:
        try:
            results[name] = run_pass(
                conn, name, project_id,
                dry_run=dry_run, cross_project=cross_project,
            )
        except RuntimeError:
            # Logged inside run_pass; continue to next pass.
            results[name] = PassResult(metadata={"error": "pass failed"})
    return results


# ── Run log helpers ──────────────────────────────────────────────────────


def _start_run_log(conn: Any, pass_name: str, project_id: Any) -> int:
    """Insert a started-but-not-completed run log row. Returns the row id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO devbrain.cognify_run_log "
            "(pass_name, project_id, started_at) "
            "VALUES (%s, %s, now()) "
            "RETURNING id",
            (pass_name, project_id),
        )
        log_id = cur.fetchone()[0]
    conn.commit()
    return log_id


def _complete_run_log(
    conn: Any,
    log_id: int,
    result: PassResult,
    error: str | None,
    *,
    dry_run: bool,
) -> None:
    """Mark a run log row as completed. Merges pass metadata into the JSONB.

    PHI constraint: result.metadata must NOT contain raw memory.content.
    The orchestrator trusts pass implementations to honour this.
    """
    meta = dict(result.metadata or {})
    if dry_run:
        meta["dry_run"] = True
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.cognify_run_log "
            "SET completed_at = now(), "
            "    rows_processed = %s, "
            "    llm_calls = %s, "
            "    error = %s, "
            "    metadata = %s::jsonb "
            "WHERE id = %s",
            (
                result.rows_processed,
                result.llm_calls,
                error,
                json.dumps(meta),
                log_id,
            ),
        )
    conn.commit()
