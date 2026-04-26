"""Attribute orphan claude_code raw_sessions + chunks via source_path.

Background
----------
Older ingest runs (before the source_path-aware adapter landed) wrote
``raw_sessions`` rows for ``claude_code`` transcripts without filling
``project_id``. Every chunk derived from one of those orphan sessions
also carries ``project_id IS NULL``. This breaks any project-scoped
read path (notably the P2.c memory backfill, which can't migrate rows
without a project FK).

Strategy
--------
* **Decode the source_path** — claude_code stores its transcripts under
  ``~/.claude/projects/-<encoded-path>/<sess>.jsonl`` where the
  encoded path is the absolute project directory with each ``/``
  replaced by ``-``. Reversing that gives us a file-system path we can
  match against the operator's ``factory.project_paths`` config.
* **Longest-prefix match** against ``FACTORY_CONFIG["project_paths"]``
  (``{slug: path}``). Slugs map to project rows via
  ``devbrain.projects.slug``.
* **Per-batch transactions, keyset-paged scans** — same shape as
  ``backfill_memory.py``: ``WHERE id > %s ORDER BY id LIMIT N``,
  best-effort recovery on batch failure, ``last_id`` always advances.
* **Idempotency, dual-layered** — the SELECT filters
  ``project_id IS NULL`` (so a re-run scans zero rows once everything
  resolvable has been attributed), and the UPDATE re-asserts the same
  predicate (``WHERE id = %s AND project_id IS NULL``) so a SELECT/
  UPDATE race against a concurrent attributor can't stomp an existing
  attribution.
* **Per-row UPDATE with cur.rowcount==1 check** — counter is the sum
  of true write successes, not predicted attributions. A row that was
  attributed by a concurrent process between our SELECT and our
  UPDATE will be silently skipped.
* **Schema-existence guard at entry point** — ``_ensure_schema``
  verifies the tables we touch exist before any scan. Direct callers
  (tests, scripts) get the same protection as the CLI. Past-review
  lesson from P2.c: surface schema drift loudly at the entry point
  rather than buried in mid-batch errors.
* **default_project_slug fallback is opt-in** — only fires for rows
  whose ``source_path`` cannot be decoded or whose decoded path
  matches no configured project. Slug is resolved once at entry; an
  unknown slug raises ``ValueError`` so the operator notices
  immediately rather than discovering empty fallbacks mid-run.

Worktree convention
-------------------
The factory creates per-job worktrees at
``~/devbrain-worktrees/<job-id>/`` (sibling of ``~/devbrain``). The
naive decode of ``-Users-x-devbrain-worktrees-<id>`` lands on
``/Users/x/devbrain/worktrees/<id>`` (a path that doesn't exist). We
detect this with a regex and re-glue the missing dash so the result
matches the actual checkout.

Out of scope
------------
* ``devbrain.memory`` (P2.d task #25) — chunks/sessions only.
* Non-claude_code adapters — their ``source_path`` shapes are
  different and out of scope for this attribution pass.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from config import FACTORY_CONFIG

logger = logging.getLogger(__name__)


# Sentinel UUID smaller than any real UUID — keyset pagination starts here.
_ZERO_UUID = "00000000-0000-0000-0000-000000000000"

# Detects the naive decode of factory-worktree paths so we can re-glue
# the missing ``devbrain``→``-worktrees`` dash. Matches paths shaped like
# ``<anything>/worktrees/<hex-or-uuid-id>[/<rest>]``. The id class allows
# 4-40 hex/dash characters which covers both short (8-char) and full
# (36-char with dashes) worktree IDs.
_WORKTREE_TAIL_RE = re.compile(
    r"^(.+)/worktrees/([0-9a-f][0-9a-f-]{3,39})(/.*)?$"
)

# claude_code stores per-project transcript directories under here.
_CLAUDE_PROJECTS_PREFIX = "/.claude/projects/"


# ─── Pure helpers (no DB) ────────────────────────────────────────────────────


def decode_claude_code_path(source_path: str) -> str | None:
    """Decode a claude_code ``source_path`` to its project directory.

    claude_code writes transcripts to
    ``~/.claude/projects/-<encoded-dir>/<session>.jsonl`` where
    ``<encoded-dir>`` is the absolute project directory with each
    ``/`` rewritten as ``-`` (so a leading ``/`` becomes the leading
    ``-``). Reverses that mapping and re-glues the factory worktree
    convention (``<project>-worktrees/<id>``) which a naive decode
    would land at ``<project>/worktrees/<id>``.

    Args:
        source_path: absolute path to the per-session transcript file.

    Returns:
        The decoded project directory, or ``None`` if the input
        doesn't live under ``~/.claude/projects/`` or the encoded
        segment is degenerate (empty or all dashes).
    """
    if not source_path:
        return None

    home = str(Path.home())
    prefix = home + _CLAUDE_PROJECTS_PREFIX
    if not source_path.startswith(prefix):
        return None

    rest = source_path[len(prefix):]
    # First "/"-segment after the prefix is the encoded directory.
    encoded = rest.split("/", 1)[0]
    stripped = encoded.lstrip("-")
    if not stripped:
        # Degenerate: encoded was empty or all dashes. Nothing to
        # decode — caller may opt to fall back to default_project.
        return None

    decoded = "/" + stripped.replace("-", "/")

    m = _WORKTREE_TAIL_RE.match(decoded)
    if m:
        decoded = f"{m.group(1)}-worktrees/{m.group(2)}{m.group(3) or ''}"

    return decoded


def resolve_project_id(db, project_dir: str) -> str | None:
    """Map a decoded project directory to a ``devbrain.projects.id``.

    Reads ``FACTORY_CONFIG["project_paths"]`` (``{slug: path}``),
    expands ``~`` per value, and finds the longest configured path
    that is a prefix of ``project_dir``. Looks up the matching slug's
    UUID in ``devbrain.projects``.

    The longest-prefix step lets nested checkouts (``/code/foo`` vs
    ``/code/foo/sub``) coexist in config without ambiguity. The
    ``+ "/"`` boundary check prevents ``/foo`` from matching
    ``/foobar``.

    Returns ``None`` if nothing matches or if the matched slug is
    missing from ``devbrain.projects``.
    """
    mappings = FACTORY_CONFIG.get("project_paths") or {}
    if not mappings:
        return None

    # Expand ~ but DO NOT call .resolve() — test paths and operator
    # configs aren't required to exist on disk.
    expanded = {
        slug: str(Path(p).expanduser()) for slug, p in mappings.items()
    }

    project_dir = project_dir.rstrip("/")

    candidates = [
        (path, slug) for slug, path in expanded.items()
        if project_dir == path or project_dir.startswith(path + "/")
    ]
    if not candidates:
        return None

    candidates.sort(key=lambda t: len(t[0]), reverse=True)
    _path, slug = candidates[0]

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.projects WHERE slug = %s",
            (slug,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return str(row[0])


# ─── DB-touching helpers ─────────────────────────────────────────────────────


def _ensure_schema(db) -> None:
    """Verify the tables we read/write exist before any scan.

    Raises ``RuntimeError`` with a hint to run ``bin/devbrain migrate``
    if any of ``raw_sessions``, ``chunks``, ``projects`` is missing.
    Called at the top of every public entry point so direct callers
    (tests, scripts) get the same protection as the CLI.
    """
    required = ("raw_sessions", "chunks", "projects")
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'devbrain' AND table_name = ANY(%s)",
            (list(required),),
        )
        present = {row[0] for row in cur.fetchall()}
    missing = [t for t in required if t not in present]
    if missing:
        raise RuntimeError(
            f"devbrain.{', devbrain.'.join(missing)} table(s) missing — "
            "run `bin/devbrain migrate` first."
        )


def _resolve_default_project_id(db, slug: str | None) -> str | None:
    """Resolve ``--default-project SLUG`` to a project UUID once per run.

    Returns ``None`` if no slug was passed (the common case — fallback
    is opt-in). Raises ``ValueError`` if a slug was passed but no
    matching ``devbrain.projects`` row exists, so the operator notices
    immediately rather than discovering silently-empty fallbacks
    mid-run.
    """
    if slug is None:
        return None
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.projects WHERE slug = %s", (slug,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(
            f"--default-project slug '{slug}' not found in "
            "devbrain.projects; pass a slug that exists."
        )
    return str(row[0])


# ─── Sessions ────────────────────────────────────────────────────────────────


def attribute_orphan_sessions(
    db,
    *,
    batch_size: int = 1000,
    dry_run: bool = False,
    default_project_slug: str | None = None,
) -> dict:
    """Attribute orphan claude_code raw_sessions via source_path decode.

    Scans ``raw_sessions WHERE project_id IS NULL AND source_app =
    'claude_code'`` keyset-paged, decodes each row's ``source_path`` to
    its project directory, looks up the project UUID via
    ``FACTORY_CONFIG["project_paths"]`` longest-prefix match, and
    UPDATEs the row with the resolved id. Rows with undecodable paths
    or no matching project fall back to ``default_project_slug`` (if
    configured) or count as ``unrecoverable``.

    Args:
        db: ``FactoryDB``.
        batch_size: rows per batch for keyset-paged scans.
        dry_run: if True, no UPDATEs are issued; counters report what
            *would* be attributed (each predicted attribution increments
            the relevant counter exactly once).
        default_project_slug: optional fallback project slug. Resolved
            once at entry; raises ``ValueError`` if the slug is unknown.

    Returns:
        ``{scanned, attributed, unrecoverable, fallback_to_default,
        batch_failures, duration_s}``.
    """
    _ensure_schema(db)
    default_project_id = _resolve_default_project_id(db, default_project_slug)

    counts = {
        "scanned": 0,
        "attributed": 0,
        "unrecoverable": 0,
        "fallback_to_default": 0,
        "batch_failures": 0,
        "duration_s": 0.0,
    }
    started = time.perf_counter()
    last_id = _ZERO_UUID

    select_sql = (
        "SELECT id, source_path FROM devbrain.raw_sessions "
        "WHERE id > %s AND project_id IS NULL AND source_app = 'claude_code' "
        "ORDER BY id LIMIT %s"
    )
    update_sql = (
        "UPDATE devbrain.raw_sessions SET project_id = %s "
        "WHERE id = %s AND project_id IS NULL"
    )

    while True:
        # SELECT in its own short txn. A SELECT failure (rare —
        # connection blip) breaks the loop: we have no last_id to
        # advance to and re-running will retry the same batch.
        try:
            with db._conn() as conn, conn.cursor() as cur:
                cur.execute(select_sql, (last_id, batch_size))
                rows = cur.fetchall()
        except Exception as exc:
            logger.warning(
                "attribute_orphan_sessions SELECT failed (last_id=%s): %s",
                last_id, exc,
            )
            counts["batch_failures"] += 1
            break

        if not rows:
            break

        # Resolve every row's project_id outside the write txn so a
        # buggy decoder/resolver can't roll back a healthy batch.
        resolved: list[tuple[str, str, bool]] = []  # (row_id, pid, used_fallback)
        for row_id, source_path in rows:
            counts["scanned"] += 1
            decoded = decode_claude_code_path(source_path)
            project_id = (
                resolve_project_id(db, decoded) if decoded else None
            )
            if project_id is not None:
                resolved.append((str(row_id), project_id, False))
            elif default_project_id is not None:
                resolved.append((str(row_id), default_project_id, True))
            else:
                counts["unrecoverable"] += 1

        # Always advance last_id even if nothing in this page resolved
        # — otherwise we'd loop forever on a page of unrecoverables.
        last_id = str(rows[-1][0])

        if dry_run:
            # Predicted attributions; count once per resolved row.
            for _row_id, _pid, used_fallback in resolved:
                if used_fallback:
                    counts["fallback_to_default"] += 1
                else:
                    counts["attributed"] += 1
            if len(rows) < batch_size:
                break
            continue

        if not resolved:
            if len(rows) < batch_size:
                break
            continue

        # One UPDATE per row so cur.rowcount cleanly distinguishes a
        # real write (==1) from an idempotency-skip (==0, e.g. a
        # concurrent attributor beat us between SELECT and UPDATE).
        # Accumulate deltas locally and fold into counts only after
        # commit() returns — otherwise a mid-flush failure rolls back
        # the writes but leaves the counters overstated.
        batch_attributed = 0
        batch_fallback = 0
        try:
            with db._conn() as conn, conn.cursor() as cur:
                for row_id, pid, used_fallback in resolved:
                    cur.execute(update_sql, (pid, row_id))
                    if cur.rowcount == 1:
                        if used_fallback:
                            batch_fallback += 1
                        else:
                            batch_attributed += 1
                conn.commit()
            counts["attributed"] += batch_attributed
            counts["fallback_to_default"] += batch_fallback
        except Exception as exc:
            logger.warning(
                "attribute_orphan_sessions batch UPDATE failed "
                "(last_id=%s, size=%d): %s",
                last_id, len(resolved), exc,
            )
            counts["batch_failures"] += 1
            # last_id already advanced above; operator re-runs to mop up.

        if len(rows) < batch_size:
            break

    counts["duration_s"] = round(time.perf_counter() - started, 3)
    return counts


# ─── Chunks ──────────────────────────────────────────────────────────────────


def attribute_orphan_chunks(
    db,
    *,
    batch_size: int = 1000,
    dry_run: bool = False,
) -> dict:
    """Attribute orphan chunks by inheriting their parent session's project.

    Scans ``chunks WHERE project_id IS NULL`` keyset-paged, joins to
    ``raw_sessions`` via ``chunks.source_id = raw_sessions.id``, and
    UPDATEs the chunk with the parent's ``project_id``. Chunks whose
    parent session also has ``project_id IS NULL`` (orphan-on-orphan)
    are counted in ``parent_still_null`` — the operator should run
    ``attribute_orphan_sessions`` first (or use ``attribute_all`` which
    does that for them).

    Args:
        db: ``FactoryDB``.
        batch_size: rows per batch for keyset-paged scans.
        dry_run: if True, no UPDATEs are issued; counters report what
            *would* be attributed.

    Returns:
        ``{scanned, attributed, parent_still_null, batch_failures,
        duration_s}``.
    """
    _ensure_schema(db)

    counts = {
        "scanned": 0,
        "attributed": 0,
        "parent_still_null": 0,
        "batch_failures": 0,
        "duration_s": 0.0,
    }
    started = time.perf_counter()
    last_id = _ZERO_UUID

    # LEFT JOIN so chunks whose source_id has no matching raw_sessions
    # row (or whose source_id is NULL) still appear and are counted in
    # parent_still_null rather than silently filtered out.
    select_sql = (
        "SELECT c.id, rs.project_id FROM devbrain.chunks c "
        "LEFT JOIN devbrain.raw_sessions rs ON c.source_id = rs.id "
        "WHERE c.id > %s AND c.project_id IS NULL "
        "ORDER BY c.id LIMIT %s"
    )
    update_sql = (
        "UPDATE devbrain.chunks SET project_id = %s "
        "WHERE id = %s AND project_id IS NULL"
    )

    while True:
        try:
            with db._conn() as conn, conn.cursor() as cur:
                cur.execute(select_sql, (last_id, batch_size))
                rows = cur.fetchall()
        except Exception as exc:
            logger.warning(
                "attribute_orphan_chunks SELECT failed (last_id=%s): %s",
                last_id, exc,
            )
            counts["batch_failures"] += 1
            break

        if not rows:
            break

        resolved: list[tuple[str, str]] = []  # (chunk_id, parent_pid)
        for chunk_id, parent_pid in rows:
            counts["scanned"] += 1
            if parent_pid is None:
                counts["parent_still_null"] += 1
                continue
            resolved.append((str(chunk_id), str(parent_pid)))

        last_id = str(rows[-1][0])

        if dry_run:
            counts["attributed"] += len(resolved)
            if len(rows) < batch_size:
                break
            continue

        if not resolved:
            if len(rows) < batch_size:
                break
            continue

        # Accumulate batch delta locally and fold into counts only
        # after commit() returns — otherwise a mid-flush failure rolls
        # back the writes but leaves the counter overstated.
        batch_attributed = 0
        try:
            with db._conn() as conn, conn.cursor() as cur:
                for chunk_id, pid in resolved:
                    cur.execute(update_sql, (pid, chunk_id))
                    if cur.rowcount == 1:
                        batch_attributed += 1
                conn.commit()
            counts["attributed"] += batch_attributed
        except Exception as exc:
            logger.warning(
                "attribute_orphan_chunks batch UPDATE failed "
                "(last_id=%s, size=%d): %s",
                last_id, len(resolved), exc,
            )
            counts["batch_failures"] += 1

        if len(rows) < batch_size:
            break

    counts["duration_s"] = round(time.perf_counter() - started, 3)
    return counts


# ─── Aggregator ──────────────────────────────────────────────────────────────


def attribute_all(
    db,
    *,
    batch_size: int = 1000,
    dry_run: bool = False,
    default_project_slug: str | None = None,
) -> dict:
    """Run sessions then chunks, in that order.

    Order matters: ``attribute_orphan_chunks`` inherits its
    ``project_id`` from the parent session, so newly-attributed
    sessions in the same run propagate to their chunks.

    Returns ``{"sessions": <dict>, "chunks": <dict>}``.
    """
    _ensure_schema(db)
    sessions = attribute_orphan_sessions(
        db,
        batch_size=batch_size,
        dry_run=dry_run,
        default_project_slug=default_project_slug,
    )
    chunks = attribute_orphan_chunks(
        db, batch_size=batch_size, dry_run=dry_run,
    )
    return {"sessions": sessions, "chunks": chunks}
