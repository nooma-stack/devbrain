"""Click subcommands for the devbrain audit workflow.

Wired into factory/cli.py via late imports — keeps cli.py from importing
the DB layer at module load time.

Provides:

- ``devbrain audit verify`` — walks devbrain.memory_ledger via the SQL-side
  verify_chain() function and reports the first divergence (or success).

Suitable for cron / CI: exits 0 when the requested range is intact, exits 1
when a break is found, exits 2 on operational failure (DB unreachable,
ledger table missing, etc.). The fail-then-exit pattern matches the
existing devdoctor / migration runners.

Phase 3 / Atlas Step 3 — see docs/plans/2026-04-29-phase-3-discipline-layer.md.
"""

from __future__ import annotations

import json as _json
import logging
import sys
from typing import Any

import click

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _connect():
    """Return a fresh psycopg2 connection from FACTORY_CONFIG.

    Imported lazily so `--help` doesn't load psycopg2 / config.
    """
    from config import DATABASE_URL  # type: ignore[import-not-found]
    import psycopg2  # type: ignore[import-not-found]

    return psycopg2.connect(DATABASE_URL)


def _ensure_ledger_exists(conn) -> bool:
    """Return True iff devbrain.memory_ledger exists in this database.

    A missing ledger is operational-failure territory (migration 015
    hasn't run), not "intact" or "broken". We return False and let the
    caller print a helpful error.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'devbrain' AND table_name = 'memory_ledger'
            """
        )
        return cur.fetchone() is not None


def _project_slug_to_seq_range(conn, project_slug: str) -> tuple[int, int] | None:
    """Resolve (min_seq, max_seq) for a project's ledger entries.

    Returns None if the project has zero ledger entries (caller should
    treat as 'intact-trivially').
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT MIN(seq), MAX(seq) FROM devbrain.memory_ledger WHERE project_slug = %s",
            (project_slug,),
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            return None
        return int(row[0]), int(row[1])


def _verify(
    conn,
    start_seq: int,
    end_seq: int | None,
) -> list[dict[str, Any]]:
    """Call devbrain.verify_chain() and return any break rows."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT broken_at_seq, expected_hash, actual_hash, reason "
            "FROM devbrain.verify_chain(%s, %s)",
            (start_seq, end_seq),
        )
        cols = ("broken_at_seq", "expected_hash", "actual_hash", "reason")
        return [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]


def _count_in_range(conn, start_seq: int, end_seq: int | None, project_slug: str | None) -> int:
    where = ["seq >= %s"]
    params: list[Any] = [start_seq]
    if end_seq is not None:
        where.append("seq <= %s")
        params.append(end_seq)
    if project_slug:
        where.append("project_slug = %s")
        params.append(project_slug)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) FROM devbrain.memory_ledger WHERE {' AND '.join(where)}",
            params,
        )
        return int(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# Command: devbrain audit verify
# ---------------------------------------------------------------------------


@click.group(name="audit")
def audit_group() -> None:
    """Audit the devbrain.memory_ledger hash chain for tamper detection."""


@audit_group.command(name="verify")
@click.option(
    "--project",
    "project_slug",
    default=None,
    help="Restrict verification to a single project's ledger entries.",
)
@click.option(
    "--start-seq",
    type=int,
    default=None,
    help="First seq to verify. Defaults to 1 (or the project's min seq when --project is set).",
)
@click.option(
    "--end-seq",
    type=int,
    default=None,
    help="Last seq to verify. Defaults to the latest seq.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit JSON instead of human-readable output. Suitable for cron / piping.",
)
def verify_cmd(
    project_slug: str | None,
    start_seq: int | None,
    end_seq: int | None,
    as_json: bool,
) -> None:
    """Verify the memory_ledger hash chain.

    Exit codes:
      0 — chain intact across the requested range
      1 — chain break detected (see output for first broken seq)
      2 — operational failure (DB unreachable, ledger table missing, …)
    """
    try:
        conn = _connect()
    except Exception as exc:
        msg = f"Could not connect to DevBrain DB: {exc}"
        if as_json:
            click.echo(_json.dumps({"status": "operational_failure", "error": msg}))
        else:
            click.echo(f"✗ {msg}", err=True)
        sys.exit(2)

    try:
        if not _ensure_ledger_exists(conn):
            msg = (
                "devbrain.memory_ledger does not exist. "
                "Run `devbrain migrate` to apply migration 015."
            )
            if as_json:
                click.echo(
                    _json.dumps({"status": "operational_failure", "error": msg})
                )
            else:
                click.echo(f"✗ {msg}", err=True)
            sys.exit(2)

        # Resolve the seq range. When --project is set without --start-seq,
        # use the project's min so we don't waste effort verifying earlier
        # rows from other projects.
        if project_slug and start_seq is None:
            range_ = _project_slug_to_seq_range(conn, project_slug)
            if range_ is None:
                # Project has zero ledger entries — trivially intact.
                if as_json:
                    click.echo(
                        _json.dumps(
                            {
                                "status": "intact",
                                "rows_verified": 0,
                                "project": project_slug,
                                "note": "no ledger entries for project",
                            }
                        )
                    )
                else:
                    click.echo(
                        f"✓ {project_slug}: no ledger entries (trivially intact)"
                    )
                sys.exit(0)
            start_seq = range_[0]
            if end_seq is None:
                end_seq = range_[1]

        if start_seq is None:
            start_seq = 1

        breaks = _verify(conn, start_seq, end_seq)
        rows_verified = _count_in_range(conn, start_seq, end_seq, project_slug)

        if not breaks:
            if as_json:
                click.echo(
                    _json.dumps(
                        {
                            "status": "intact",
                            "rows_verified": rows_verified,
                            "start_seq": start_seq,
                            "end_seq": end_seq,
                            "project": project_slug,
                        }
                    )
                )
            else:
                scope = f" for {project_slug}" if project_slug else ""
                click.echo(
                    f"✓ Ledger intact{scope} ({rows_verified} rows verified, "
                    f"seq {start_seq}..{end_seq if end_seq is not None else 'latest'})"
                )
            sys.exit(0)

        # Break(s) found.
        if as_json:
            click.echo(
                _json.dumps(
                    {
                        "status": "broken",
                        "breaks": breaks,
                        "rows_verified": rows_verified,
                        "start_seq": start_seq,
                        "end_seq": end_seq,
                        "project": project_slug,
                    }
                )
            )
        else:
            for b in breaks:
                click.echo(
                    f"✗ Broken at seq {b['broken_at_seq']}: {b['reason']}\n"
                    f"  expected: {b['expected_hash']}\n"
                    f"  actual:   {b['actual_hash']}",
                    err=True,
                )
        sys.exit(1)

    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public registration hook (called from factory/cli.py)
# ---------------------------------------------------------------------------


def register(cli_group: click.Group) -> None:
    """Wire the audit-CLI command group onto the parent click group."""
    cli_group.add_command(audit_group)
