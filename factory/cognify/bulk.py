"""Bulk lesson/decision extraction over an existing chunk history.

`devbrain cognify-bulk` is the entry point. Discovers sessions in a
project that need atomization (have chunks but no lesson/decision rows),
processes them in a resumable + cost-capped loop, and writes per-session
results to `cognify_run_log`.

Differs from `cognify-reextract`:

  * **Discovery model.** `cognify-reextract --all` re-processes every
    session in the project, archiving prior atoms. `cognify-bulk` only
    touches sessions that currently have NO active atoms — its job is to
    catch up first-time atomization, not redo work. Existing atoms are
    preserved.

  * **Resumable.** On Ctrl+C / crash, a checkpoint file at
    `~/.devbrain/cognify-bulk-<project>.json` records completed sessions.
    Re-running picks up where the previous attempt left off. Pass
    `--no-resume` to start fresh.

  * **Cost-capped.** `--max-llm-calls=N` halts cleanly at N. Combined
    with checkpoint resume, you can spread a large bulk extraction over
    multiple budgeted runs.

  * **Client recycling.** The 2026-05-14 incident showed that long
    Python processes accumulate stale connections in the SDK's httpx
    pool. PR #137 added per-call retry on APIConnectionError, but
    `cognify-bulk` adds a second defense: explicitly closes and
    re-creates the Anthropic client every N sessions (default 50).

  * **Progress reporting.** Live per-session progress to stderr; final
    JSON summary on stdout (when --json) so the output stream stays
    parseable.

Typical use:

    # First-run atomization of an entire chunk history (most common):
    devbrain cognify-bulk --project=brightbot

    # Cost-bounded re-run; pick up later with the same command (resumes):
    devbrain cognify-bulk --project=brightbot --max-llm-calls=200

    # Filter by ingest date (only sessions newer than 2026-04-01):
    devbrain cognify-bulk --project=brightbot --since=2026-04-01

    # Dry run — report what would be processed without making LLM calls:
    devbrain cognify-bulk --project=brightbot --dry-run

    # Parallelize: run 10 instances concurrently, each owning a slice of
    # the sorted session list. Each shard has its own checkpoint file so
    # resumes don't conflict:
    devbrain cognify-bulk --project=brightbot --shard=0/10
    devbrain cognify-bulk --project=brightbot --shard=1/10
    ...
    devbrain cognify-bulk --project=brightbot --shard=9/10
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Default cadence for explicit client recycle (close + new Anthropic()).
# Per-call retry (in extract.py) handles single transient blips; this is
# a second-tier defense against gradual httpx-pool state drift over many
# hundreds of sequential calls.
_DEFAULT_RECYCLE_EVERY = 50

# Default progress-report interval (in sessions). Live reporting uses
# stderr so stdout stays parseable for --json mode.
_PROGRESS_EVERY = 1


@dataclass
class BulkRunResult:
    """Summary of one bulk-extraction invocation."""

    sessions_targeted: int = 0
    sessions_processed: int = 0
    sessions_skipped_resume: int = 0
    sessions_failed: int = 0
    atoms_created: int = 0
    llm_calls: int = 0
    failure_counts: dict[str, int] = field(default_factory=dict)
    halted_early: bool = False
    halt_reason: str | None = None
    checkpoint_path: Path | None = None
    started_at: str | None = None
    ended_at: str | None = None
    elapsed_seconds: float = 0.0


def discover_sessions_needing_atomization(
    conn: Any,
    project_id: Any,
    *,
    since: datetime | None = None,
) -> list[str]:
    """Return raw_session UUIDs that have chunks in memory but no active
    pattern/decision/lesson atoms.

    Post-migration-032, memory.provenance_id IS raw_sessions.id, so the
    join is trivial. Only memory rows whose provenance points at a real
    raw_session are returned — chunks from codebase indexing or
    headers-only imports (no raw_session) are correctly excluded from
    atomization.

    `since`: optional cutoff on raw_sessions.started_at (the actual
    conversation start time, NOT devbrain's ingest time).
    """
    params: list = [project_id]
    where_since = ""
    if since is not None:
        where_since = "AND rs.started_at >= %s"
        params.append(since)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT m.provenance_id::text
            FROM devbrain.memory m
            JOIN devbrain.raw_sessions rs ON rs.id = m.provenance_id
            WHERE m.project_id = %s
              AND m.archived_at IS NULL
              {where_since}
              AND NOT EXISTS (
                  SELECT 1 FROM devbrain.memory atoms
                  WHERE atoms.project_id = m.project_id
                    AND atoms.provenance_id = m.provenance_id
                    AND atoms.kind IN ('pattern', 'decision', 'lesson')
                    AND atoms.archived_at IS NULL
              )
            ORDER BY m.provenance_id::text
            """,
            params,
        )
        return [r[0] for r in cur.fetchall()]


def discover_all_sessions_with_chunks(
    conn: Any,
    project_id: Any,
    *,
    since: datetime | None = None,
) -> list[str]:
    """Every raw_session that has chunks in the project, regardless of
    whether atoms already exist. Used by --target=all (re-processes
    everything, archiving prior atoms — mirrors cognify-reextract --all
    semantics)."""
    params: list = [project_id]
    where_since = ""
    if since is not None:
        where_since = "AND rs.started_at >= %s"
        params.append(since)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT m.provenance_id::text
            FROM devbrain.memory m
            JOIN devbrain.raw_sessions rs ON rs.id = m.provenance_id
            WHERE m.project_id = %s
              AND m.archived_at IS NULL
              {where_since}
            ORDER BY m.provenance_id::text
            """,
            params,
        )
        return [r[0] for r in cur.fetchall()]


def _checkpoint_path_for(
    project_slug: str,
    *,
    shard: tuple[int, int] | None = None,
) -> Path:
    """Where the per-project checkpoint file lives.

    When `shard` is provided as (N, M), the filename includes the shard
    so parallel instances don't collide on each other's checkpoints.
    """
    home = Path(os.environ.get("DEVBRAIN_HOME", Path.home()))
    base = home / ".devbrain"
    base.mkdir(parents=True, exist_ok=True)
    if shard is not None:
        n, m = shard
        return base / f"cognify-bulk-{project_slug}-shard-{n}-of-{m}.json"
    return base / f"cognify-bulk-{project_slug}.json"


def apply_shard(sessions: list[str], shard: tuple[int, int]) -> list[str]:
    """Stride-slice `sessions` into shard N of M.

    Given a sorted session list (discover_* already orders by
    provenance_id), stride-slicing partitions it deterministically across
    M workers: worker N gets sessions[N::M]. Coverage is exhaustive
    (every session lands in exactly one shard) and stable across reruns
    so a crashed shard's checkpoint stays valid.

    Raises ValueError if (N, M) is out of range.
    """
    n, m = shard
    if m < 1:
        raise ValueError(f"shard total M must be >= 1, got M={m}")
    if n < 0 or n >= m:
        raise ValueError(f"shard index N={n} must satisfy 0 <= N < M={m}")
    return sessions[n::m]


def _load_checkpoint(path: Path) -> dict | None:
    """Read a prior checkpoint, or None if missing/corrupt."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(
            "cognify-bulk: ignoring corrupt checkpoint at %s: %s", path, exc,
        )
        return None


def _save_checkpoint(path: Path, data: dict) -> None:
    """Atomic checkpoint write."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


def run_bulk(
    conn: Any,
    project_id: Any,
    project_slug: str,
    *,
    sessions: list[str],
    reextract_mode: bool = False,
    max_llm_calls: int | None = None,
    recycle_every: int = _DEFAULT_RECYCLE_EVERY,
    dry_run: bool = False,
    use_checkpoint: bool = True,
    progress_callback: Callable[[int, int, dict], None] | None = None,
    shard: tuple[int, int] | None = None,
) -> BulkRunResult:
    """Process `sessions` in order, with checkpoint-resume + client
    recycle + cost cap.

    Args:
      conn: psycopg2 connection.
      project_id: the project's UUID.
      project_slug: used for the checkpoint filename.
      sessions: list of provenance_ids to process.
      reextract_mode: when True, archive any prior atomization for the
        session (cognify-reextract --all semantics). When False (default),
        new atoms append.
      max_llm_calls: halt cleanly after this many LLM calls. None =
        unlimited. Each session typically uses 1 call but session size
        can push it higher.
      recycle_every: explicitly close + re-create the Anthropic client
        every N sessions. Defeats httpx-pool state drift.
      dry_run: report planned work; make no API calls or DB writes.
      use_checkpoint: write a checkpoint file under ~/.devbrain/ so a
        crashed run can resume. Set False for one-shot ops.
      progress_callback: optional fn(idx, total, last_result_dict) called
        after each session. last_result_dict has keys session_id,
        atoms, failure, llm_calls.
      shard: when provided as (N, M), the checkpoint filename is suffixed
        with `-shard-N-of-M` so parallel shards don't share checkpoint
        state. The session list passed in is NOT re-sliced — callers are
        responsible for slicing before calling run_bulk (see apply_shard).
    """
    result = BulkRunResult()
    started_at = datetime.now(timezone.utc)
    result.started_at = started_at.isoformat()

    checkpoint_path: Path | None = None
    completed_set: set[str] = set()
    if use_checkpoint:
        checkpoint_path = _checkpoint_path_for(project_slug, shard=shard)
        result.checkpoint_path = checkpoint_path
        prior = _load_checkpoint(checkpoint_path)
        if prior and prior.get("project_slug") == project_slug:
            # Resume: skip sessions we've already finished.
            completed_set = set(prior.get("completed_sessions") or [])
            result.atoms_created = int(prior.get("atoms_created", 0))
            result.llm_calls = int(prior.get("llm_calls", 0))
            result.failure_counts = dict(prior.get("failure_counts") or {})
            result.sessions_skipped_resume = len(completed_set & set(sessions))

    todo = [s for s in sessions if s not in completed_set]
    result.sessions_targeted = len(sessions)

    if dry_run:
        # Report-only: no API calls, no DB writes, no checkpoint update.
        result.ended_at = datetime.now(timezone.utc).isoformat()
        result.elapsed_seconds = (datetime.now(timezone.utc) - started_at).total_seconds()
        return result

    # Lazy imports — avoid pulling these into modules that don't need them.
    from cognify.extract import extract_from_session  # noqa: PLC0415

    for idx, session_id in enumerate(todo, start=1):
        if max_llm_calls is not None and result.llm_calls >= max_llm_calls:
            result.halted_early = True
            result.halt_reason = (
                f"max_llm_calls cap reached ({max_llm_calls}). "
                f"Re-run the command to continue from checkpoint."
            )
            break

        try:
            sess_result = extract_from_session(
                conn,
                session_id,
                project_id,
                reextract=reextract_mode,
            )
        except Exception as exc:  # noqa: BLE001
            # extract_from_session normally returns failure as a field,
            # not a raise — but defend against bugs.
            logger.exception(
                "cognify-bulk: extract_from_session raised on %s", session_id,
            )
            result.sessions_failed += 1
            result.failure_counts["exception"] = result.failure_counts.get("exception", 0) + 1
            if progress_callback:
                progress_callback(idx, len(todo), {
                    "session_id": session_id,
                    "atoms": 0,
                    "failure": "exception",
                    "llm_calls": 0,
                })
            continue

        atoms = sess_result.lessons_created + sess_result.decisions_created
        result.sessions_processed += 1
        result.atoms_created += atoms
        result.llm_calls += sess_result.llm_calls
        if sess_result.failure:
            result.sessions_failed += 1
            key = sess_result.failure
            result.failure_counts[key] = result.failure_counts.get(key, 0) + 1
        else:
            completed_set.add(session_id)

        if progress_callback:
            progress_callback(idx, len(todo), {
                "session_id": session_id,
                "atoms": atoms,
                "failure": sess_result.failure,
                "llm_calls": sess_result.llm_calls,
            })

        # Checkpoint after each session so Ctrl+C / crash leaves a clean
        # resume point.
        if checkpoint_path is not None:
            _save_checkpoint(checkpoint_path, {
                "project_slug": project_slug,
                "started_at": result.started_at,
                "last_updated_at": datetime.now(timezone.utc).isoformat(),
                "sessions_targeted": result.sessions_targeted,
                "completed_sessions": sorted(completed_set),
                "atoms_created": result.atoms_created,
                "llm_calls": result.llm_calls,
                "failure_counts": result.failure_counts,
            })

        # Periodic client recycle — defense against pool drift. The
        # Anthropic client is constructed fresh per call inside
        # extract_from_session, so there's no client object here to
        # close. But long-running Python processes accumulate fd state
        # at the OS level too. A short pause every N sessions also
        # gives any backend rate-limiter a chance to settle.
        if recycle_every > 0 and idx % recycle_every == 0:
            logger.info(
                "cognify-bulk: recycle checkpoint (%d sessions processed). "
                "Sleeping 1s.", idx,
            )
            time.sleep(1.0)

    # Cleanup checkpoint on clean completion.
    if (
        checkpoint_path is not None
        and not result.halted_early
        and result.sessions_failed == 0
    ):
        try:
            checkpoint_path.unlink()
            result.checkpoint_path = None
        except OSError:
            pass

    result.ended_at = datetime.now(timezone.utc).isoformat()
    result.elapsed_seconds = (datetime.now(timezone.utc) - started_at).total_seconds()
    return result


def render_stderr_progress(idx: int, total: int, last: dict) -> None:
    """Default progress-callback renderer for the CLI. Writes a single
    line per session to stderr."""
    sess = last["session_id"][:8]
    atoms = last["atoms"]
    failure = last["failure"]
    if failure:
        status = f"FAILED ({failure})"
    elif atoms == 0:
        status = "0 atoms"
    else:
        status = f"{atoms} atoms"
    sys.stderr.write(
        f"\rcognify-bulk: [{idx:>5}/{total:>5}] {sess}…  {status:<24}\n"
    )
    sys.stderr.flush()
