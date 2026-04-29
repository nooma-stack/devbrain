"""Click subcommands for the project + port-registry workflow.

Wired into factory/cli.py via late imports — keeps cli.py from importing
the DB layer at module load time. Provides:

- `devbrain create-project` — interactive walkthrough
- `devbrain archive-project` — flips status, preserves port assignments
- `devbrain reactivate-project` — undoes archive
- `devbrain ports` — table view
- `devbrain reclaim-port` — explicit transfer between projects
- `devbrain seed-ports` — one-time YAML import
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

import click

logger = logging.getLogger(__name__)


_VALID_STATUS = ("active", "inactive", "archived", "experimental")


def _team_ranges_from_config() -> dict:
    try:
        from config import FACTORY_CONFIG
        ports_cfg = FACTORY_CONFIG.get("ports") or {}
        ranges = ports_cfg.get("team_ranges") or {}
        return ranges
    except Exception as e:
        logger.debug("could not read team_ranges from config: %s", e)
        return {}


def _category_for_purpose(purpose: str) -> str:
    """Map a purpose to a team-range category.

    Heuristic mapping matching the convention from
    /Users/patrickkelly/Nooma-Stack/50Tel PBX/docs/local-dev-port-registry.md:
    - web: web|ui|frontend
    - apis: api|backend|gateway|graphql
    - db_cache: postgres|mysql|db|redis|cache|elasticsearch
    Anything else falls through to the purpose name itself (and the
    allocator will use the default base 3000 if no team range matches).
    """
    p = purpose.lower()
    if p in {"web", "ui", "frontend", "app"}:
        return "web"
    if p in {"api", "backend", "gateway", "graphql"}:
        return "apis"
    if p in {"postgres", "mysql", "db", "database", "redis", "cache", "elasticsearch", "memcached"}:
        return "db_cache"
    return p


def _prompt_yesno(prompt: str, default: bool = True) -> bool:
    return click.confirm(prompt, default=default)


def _interactive_collect_ports(
    registry,
    host: str,
    team: Optional[str],
) -> list[dict]:
    """Walk the user through purpose-by-purpose port collection.

    Returns a list of {purpose, host, port_start, port_end, needs_approval,
    reclaim_from_project, notes} dicts that the caller commits.
    """
    if not _prompt_yesno("Does this project have specific port requirements?", default=True):
        click.echo("Skipping port assignment. Project will be created without ports.")
        return []

    collected: list[dict] = []
    while True:
        purpose = click.prompt(
            "Port purpose (e.g., api, web, postgres, redis). Empty to finish",
            default="",
            show_default=False,
        ).strip()
        if not purpose:
            break

        size = click.prompt(
            f"How many ports does '{purpose}' need? (1 for a single port; >1 for a range)",
            type=int,
            default=1,
        )
        if size < 1:
            click.echo("Port count must be >= 1; skipping this purpose.")
            continue

        explicit_base = None
        if click.confirm(
            f"Do you have a specific port (or range start) for '{purpose}'?",
            default=False,
        ):
            explicit_base = click.prompt(
                f"Specific starting port for '{purpose}' on {host}",
                type=int,
            )

        try:
            suggestion = registry.suggest(
                purpose=purpose,
                host=host,
                size=size,
                team=team,
                category=_category_for_purpose(purpose),
                explicit_base=explicit_base,
            )
        except Exception as e:
            click.echo(f"  Error suggesting port: {e}", err=True)
            if not click.confirm("Continue with another purpose?", default=True):
                break
            continue

        if suggestion.needs_approval:
            click.echo(
                f"  ⚠  Port {suggestion.range.start}-{suggestion.range.end} "
                f"was previously assigned to archived project "
                f"'{suggestion.reclaim_from_project}'."
            )
            if not click.confirm(
                "  Reclaim it for this project? (Confirm only if you're sure "
                "the archived project won't be spun back up.)",
                default=False,
            ):
                click.echo("  Skipped. Try a different specific port or purpose.")
                continue

        from port_registry import format_port_range
        click.echo(
            f"  → Suggested {format_port_range(suggestion.range)} for "
            f"'{purpose}' on {host}"
        )
        if not click.confirm("  Accept?", default=True):
            continue

        notes = click.prompt(
            "  Notes (optional)", default="", show_default=False,
        ) or None

        collected.append({
            "purpose": purpose,
            "host": host,
            "port_start": suggestion.range.start,
            "port_end": suggestion.range.end,
            "needs_approval": suggestion.needs_approval,
            "reclaim_from_project": suggestion.reclaim_from_project,
            "notes": notes,
        })

    return collected


# ────────────────────────────────────────────────────────────────────────────
# Click commands — registered onto factory/cli.py via _register()
# ────────────────────────────────────────────────────────────────────────────


@click.command(name="create-project")
@click.option("--slug", prompt="Project slug (lowercase, [a-z0-9_-])", help="Unique short identifier")
@click.option("--name", prompt="Project name (full)", help="Human-readable name")
@click.option("--root-path", "root_path", prompt="Project root path", default="", help="Absolute path to repo on disk")
@click.option("--team", default=None, help="Team owning this project (e.g., nooma-stack, lhtdev)")
@click.option("--compose-project", "compose_project", default=None, help="Docker Compose project name")
@click.option(
    "--status",
    type=click.Choice(_VALID_STATUS),
    default="active",
    show_default=True,
    help="Initial project status",
)
@click.option("--host", default="localhost", show_default=True, help="Host for port assignments")
def create_project_cmd(slug, name, root_path, team, compose_project, status, host):
    """Create a new project with an interactive port-collection walkthrough."""
    from state_machine import FactoryDB
    from config import DATABASE_URL
    from port_registry import PortRegistry

    db = FactoryDB(DATABASE_URL)
    registry = PortRegistry(db, team_ranges=_team_ranges_from_config())

    # Validate slug + ensure it doesn't already exist
    import re
    if not re.fullmatch(r"[a-z0-9_-]{1,100}", slug):
        click.echo(f"Error: invalid slug {slug!r} — must match [a-z0-9_-]{{1,100}}", err=True)
        sys.exit(1)

    if not team:
        team = click.prompt("Team (optional, e.g., nooma-stack)", default="", show_default=False) or None

    # Collect ports interactively
    click.echo("")
    click.echo(f"Creating project '{slug}' ({name}) for team {team or '(none)'}.")
    click.echo("")

    port_specs = _interactive_collect_ports(registry, host=host, team=team)

    # Summary + confirm
    click.echo("")
    click.echo("──────────────────────────────────────────────────")
    click.echo(f"  Slug:            {slug}")
    click.echo(f"  Name:            {name}")
    click.echo(f"  Team:            {team or '(none)'}")
    click.echo(f"  Compose project: {compose_project or '(none)'}")
    click.echo(f"  Root path:       {root_path or '(none)'}")
    click.echo(f"  Status:          {status}")
    if port_specs:
        click.echo(f"  Ports ({len(port_specs)}):")
        for p in port_specs:
            from port_registry import format_port_range, PortRange
            r = PortRange(p["port_start"], p["port_end"])
            tag = " (reclaim)" if p.get("needs_approval") else ""
            click.echo(f"    • {p['purpose']:<20} {p['host']}:{format_port_range(r)}{tag}")
    else:
        click.echo("  Ports:           (none)")
    click.echo("──────────────────────────────────────────────────")

    if not click.confirm("Create this project?", default=True):
        click.echo("Aborted.")
        sys.exit(0)

    # Atomic insert: project row + each port assignment
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO devbrain.projects
                (slug, name, team, status, root_path, compose_project)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            [slug, name, team, status, root_path or None, compose_project],
        )
        project_id = cur.fetchone()[0]

        for p in port_specs:
            if p.get("needs_approval") and p.get("reclaim_from_project"):
                # Mark the prior assignment as torn down
                cur.execute(
                    """
                    UPDATE devbrain.port_assignments
                    SET archived_at = now()
                    WHERE host = %s
                      AND port_start <= %s AND port_end >= %s
                      AND archived_at IS NULL
                    """,
                    [p["host"], p["port_start"], p["port_end"]],
                )
            cur.execute(
                """
                INSERT INTO devbrain.port_assignments
                    (project_id, host, purpose, port_start, port_end, notes)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                [project_id, p["host"], p["purpose"], p["port_start"], p["port_end"], p.get("notes")],
            )
        conn.commit()

    click.echo(f"✅ Created project '{slug}' (id={project_id}) with {len(port_specs)} port assignment(s).")


@click.command(name="archive-project")
@click.option("--slug", required=True, help="Project slug to archive")
@click.confirmation_option(prompt="Mark this project archived? Ports stay reserved unless explicitly reclaimed.")
def archive_project_cmd(slug):
    """Mark a project archived. Port assignments stay reserved."""
    from state_machine import FactoryDB
    from config import DATABASE_URL

    db = FactoryDB(DATABASE_URL)
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE devbrain.projects
            SET status = 'archived', archived_at = now(), updated_at = now()
            WHERE slug = %s
            RETURNING id
            """,
            [slug],
        )
        row = cur.fetchone()
        if not row:
            click.echo(f"Error: no project with slug {slug!r}", err=True)
            sys.exit(1)
        conn.commit()
    click.echo(f"✅ Archived project '{slug}'. Port assignments preserved (status reflects archival).")


@click.command(name="reactivate-project")
@click.option("--slug", required=True, help="Project slug to reactivate")
@click.option(
    "--status",
    type=click.Choice(["active", "inactive", "experimental"]),
    default="active",
    show_default=True,
)
def reactivate_project_cmd(slug, status):
    """Move an archived project back to active/inactive/experimental."""
    from state_machine import FactoryDB
    from config import DATABASE_URL

    db = FactoryDB(DATABASE_URL)
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE devbrain.projects
            SET status = %s, archived_at = NULL, updated_at = now()
            WHERE slug = %s
            RETURNING id
            """,
            [status, slug],
        )
        row = cur.fetchone()
        if not row:
            click.echo(f"Error: no project with slug {slug!r}", err=True)
            sys.exit(1)
        conn.commit()
    click.echo(f"✅ Reactivated project '{slug}' (status={status}).")


@click.command(name="ports")
@click.option("--project", "project_slug", default=None, help="Filter by project slug")
@click.option("--host", default=None, help="Filter by host")
@click.option(
    "--include-archived/--exclude-archived",
    default=True,
    show_default=True,
    help="Show ports from archived projects too",
)
def ports_cmd(project_slug, host, include_archived):
    """Show all port assignments (table view)."""
    from state_machine import FactoryDB
    from config import DATABASE_URL
    from port_registry import PortRegistry, format_port_range

    db = FactoryDB(DATABASE_URL)
    registry = PortRegistry(db, team_ranges=_team_ranges_from_config())

    rows = registry.list_assignments(
        host=host, project_slug=project_slug, include_archived=include_archived,
    )
    if not rows:
        click.echo("No port assignments.")
        return

    headers = ["host", "port", "project", "status", "purpose", "notes"]
    widths = [max(len(h), 8) for h in headers]
    table_rows: list[list[str]] = []
    for r in rows:
        port_str = format_port_range(r.port_range)
        notes = r.notes or ""
        if r.archived_at:
            notes = f"(archived) {notes}".strip()
        cells = [r.host, port_str, r.project_slug, r.project_status, r.purpose, notes]
        table_rows.append(cells)
        for i, c in enumerate(cells):
            widths[i] = max(widths[i], len(str(c)))

    click.echo("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    click.echo("  ".join("-" * w for w in widths))
    for cells in table_rows:
        click.echo("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells)))


@click.command(name="assign-port")
@click.option("--slug", required=True, help="Project slug to add the port to")
@click.option("--purpose", required=True, help="What this port is for (e.g., redis, metrics, websocket)")
@click.option("--host", default="localhost", show_default=True)
@click.option("--port", "port_spec", default=None,
              help="Explicit port or range (e.g., 8080 or 20000-20100). Omit to auto-suggest.")
@click.option("--size", type=int, default=1, show_default=True,
              help="For auto-suggested ranges (e.g., 100 for 100 contiguous ports).")
@click.option("--accept-archived", is_flag=True,
              help="Auto-confirm if the suggestion lands on an archived project's range.")
@click.option("--notes", default=None, help="Optional free-form notes for the assignment.")
@click.option("--yes", is_flag=True, help="Skip the final summary confirmation.")
def assign_port_cmd(slug, purpose, host, port_spec, size, accept_archived, notes, yes):
    """Add a port to an existing project after create-project.

    Use when new port requirements come up mid-project (e.g., you realize
    the project needs Redis, a metrics endpoint, a websocket server). Reuses
    the same allocator as create-project and respects all the same invariants:
    no overlap with active or inactive projects, archived ranges only via
    explicit reclaim, and team-range conventions for auto-suggestion.

    Will refuse if:
    - The project doesn't exist (use create-project first).
    - The project is archived (reactivate first).
    - The project already has a non-archived assignment for this purpose.
    """
    from state_machine import FactoryDB
    from config import DATABASE_URL
    from port_registry import PortRegistry, PortRange, format_port_range, parse_port_spec

    db = FactoryDB(DATABASE_URL)
    registry = PortRegistry(db, team_ranges=_team_ranges_from_config())

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, status, team FROM devbrain.projects WHERE slug = %s",
            [slug],
        )
        row = cur.fetchone()
    if not row:
        click.echo(
            f"Error: no project with slug {slug!r}. "
            f"Run `devbrain create-project` first to register it.",
            err=True,
        )
        sys.exit(1)
    project_id, project_name, status, project_team = row

    if status == "archived":
        click.echo(
            f"Error: project '{slug}' is archived. Reactivate it first:\n"
            f"  devbrain reactivate-project --slug {slug}",
            err=True,
        )
        sys.exit(1)

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT port_start, port_end FROM devbrain.port_assignments
            WHERE project_id = %s AND purpose = %s AND archived_at IS NULL
            """,
            [project_id, purpose],
        )
        existing = cur.fetchone()
    if existing:
        existing_str = format_port_range(PortRange(existing[0], existing[1]))
        click.echo(
            f"Error: project '{slug}' already has '{purpose}' assigned to {existing_str}.\n"
            f"  - To keep that port: do nothing - purpose is already registered.\n"
            f"  - To change: pick a different purpose name, or run "
            f"`devbrain reclaim-port` to retire the old one explicitly.",
            err=True,
        )
        sys.exit(1)

    if port_spec:
        try:
            port_range = parse_port_spec(port_spec)
        except ValueError as e:
            click.echo(f"Error parsing port spec: {e}", err=True)
            sys.exit(1)
        all_assignments = registry.list_assignments(host=host, include_archived=True)
        archived_overlap = None
        for a in all_assignments:
            if a.project_status == "archived" or a.archived_at:
                if port_range.overlaps(a.port_range):
                    archived_overlap = a
                continue
            if port_range.overlaps(a.port_range):
                click.echo(
                    f"Error: {format_port_range(port_range)} on {host} overlaps with "
                    f"existing assignment {format_port_range(a.port_range)} for "
                    f"project '{a.project_slug}' purpose '{a.purpose}' "
                    f"(status: {a.project_status}). Pick a different port or unassign the existing one first.",
                    err=True,
                )
                sys.exit(1)
        needs_approval = archived_overlap is not None
        reclaim_from = archived_overlap.project_slug if archived_overlap else None
    else:
        try:
            suggestion = registry.suggest(
                purpose=purpose,
                host=host,
                size=size,
                team=project_team,
                category=_category_for_purpose(purpose),
            )
        except Exception as e:
            click.echo(f"Error suggesting port: {e}", err=True)
            sys.exit(1)
        port_range = suggestion.range
        needs_approval = suggestion.needs_approval
        reclaim_from = suggestion.reclaim_from_project

    if needs_approval and not accept_archived:
        click.echo(
            f"⚠  Port {format_port_range(port_range)} was previously assigned to "
            f"archived project '{reclaim_from}'."
        )
        if not click.confirm(
            "Reclaim for this project? (Confirm only if the archived project won't be spun back up.)",
            default=False,
        ):
            click.echo("Aborted.")
            sys.exit(0)

    click.echo("")
    click.echo("─" * 50)
    click.echo(f"  Project: {slug} ({project_name})")
    click.echo(f"  Purpose: {purpose}")
    click.echo(f"  Host:    {host}")
    click.echo(f"  Port:    {format_port_range(port_range)}")
    if notes:
        click.echo(f"  Notes:   {notes}")
    if reclaim_from:
        click.echo(f"  Reclaim from archived: {reclaim_from}")
    click.echo("─" * 50)

    if not yes and not click.confirm("Add this port assignment?", default=True):
        click.echo("Aborted.")
        sys.exit(0)

    with db._conn() as conn, conn.cursor() as cur:
        if needs_approval:
            cur.execute(
                """
                UPDATE devbrain.port_assignments
                SET archived_at = now()
                WHERE host = %s
                  AND port_start <= %s AND port_end >= %s
                  AND archived_at IS NULL
                """,
                [host, port_range.start, port_range.end],
            )
        cur.execute(
            """
            INSERT INTO devbrain.port_assignments
                (project_id, host, purpose, port_start, port_end, notes)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            [project_id, host, purpose, port_range.start, port_range.end, notes],
        )
        conn.commit()

    click.echo(
        f"✅ Assigned {host}:{format_port_range(port_range)} for '{purpose}' on project '{slug}'."
    )


@click.command(name="unassign-port")
@click.option("--slug", required=True, help="Project slug owning the port")
@click.option("--purpose", required=True, help="Purpose name to retire (matches the original assignment)")
@click.option("--notes", default=None,
              help="Optional explanation for the retirement (appended to the row's notes).")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def unassign_port_cmd(slug, purpose, notes, yes):
    """Retire a port assignment from an active project, preserving history.

    Sets `archived_at = now()` on the assignment row — the row is NOT deleted.
    This preserves the project's port history: if the project is later spun
    back up, agents can query its prior assignments via
    `devbrain ports --project <slug> --include-archived` and decide whether
    to reclaim the old port (if free) or pick a new one (if another project
    has since claimed it).

    Refuses if:
    - The project doesn't exist.
    - No active assignment exists for the given purpose (already retired
      or never assigned).
    """
    from state_machine import FactoryDB
    from config import DATABASE_URL
    from port_registry import PortRange, format_port_range

    db = FactoryDB(DATABASE_URL)

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, name FROM devbrain.projects WHERE slug = %s",
            [slug],
        )
        proj = cur.fetchone()
        if not proj:
            click.echo(f"Error: no project with slug {slug!r}.", err=True)
            sys.exit(1)
        project_id, project_name = proj

        cur.execute(
            """
            SELECT id, host, port_start, port_end, notes
            FROM devbrain.port_assignments
            WHERE project_id = %s AND purpose = %s AND archived_at IS NULL
            """,
            [project_id, purpose],
        )
        row = cur.fetchone()
        if not row:
            click.echo(
                f"Error: project '{slug}' has no active assignment for purpose '{purpose}'.",
                err=True,
            )
            sys.exit(1)
        assignment_id, host, port_start, port_end, existing_notes = row

    port_str = format_port_range(PortRange(port_start, port_end))

    click.echo("")
    click.echo("─" * 50)
    click.echo(f"  Project: {slug} ({project_name})")
    click.echo(f"  Purpose: {purpose}")
    click.echo(f"  Host:    {host}")
    click.echo(f"  Port:    {port_str}")
    click.echo("  Action:  retire (archived_at = now)")
    click.echo("           historical record preserved for future revival")
    if notes:
        click.echo(f"  Notes:   {notes}")
    click.echo("─" * 50)

    if not yes and not click.confirm("Retire this port assignment?", default=True):
        click.echo("Aborted.")
        sys.exit(0)

    final_notes = existing_notes or ""
    if notes:
        sep = " | " if final_notes else ""
        final_notes = f"{final_notes}{sep}retired: {notes}"

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE devbrain.port_assignments
            SET archived_at = now(),
                notes = %s
            WHERE id = %s
            """,
            [final_notes or None, assignment_id],
        )
        conn.commit()

    click.echo(
        f"✅ Retired {host}:{port_str} from '{slug}' (purpose '{purpose}'). "
        f"History preserved — view via `devbrain ports --project {slug}`."
    )


"""Compose wrapper — devbrain compose <slug> -- <docker compose args...>

Reads a project's active port assignments and exposes them as env vars
to the spawned docker-compose subprocess so Compose YAML can reference
them via ${API_PORT}, ${WEB_PORT_START}, etc. without manual config drift.
"""


def _build_compose_env(port_assignments, prefix: str = "", upper: bool = True) -> dict:
    """Build the env dict from a list of PortAssignment-like rows.

    Single port → <PURPOSE>_PORT=<value>
    Range       → <PURPOSE>_PORT_START=<value>, <PURPOSE>_PORT_END=<value>
                  (no <PURPOSE>_PORT for ranges — caller must use _START/_END)
    """
    env = {}
    for a in port_assignments:
        purpose_name = a.purpose
        if upper:
            purpose_name = purpose_name.upper()
        purpose_name = purpose_name.replace("-", "_")
        var_base = f"{prefix}{purpose_name}"
        if a.port_range.start == a.port_range.end:
            env[f"{var_base}_PORT"] = str(a.port_range.start)
        else:
            env[f"{var_base}_PORT_START"] = str(a.port_range.start)
            env[f"{var_base}_PORT_END"] = str(a.port_range.end)
    return env


@click.command(name="compose", context_settings={"ignore_unknown_options": True})
@click.argument("slug")
@click.argument("compose_args", nargs=-1, type=click.UNPROCESSED)
@click.option("--env", "env_only", is_flag=True,
              help="Print env vars as `export NAME=value` lines (sourceable) instead of running docker compose.")
@click.option("--prefix", default="", help="Prefix for env var names (e.g., 'DEVBRAIN_').")
@click.option("--upper/--no-upper", default=True, show_default=True,
              help="Uppercase the purpose name in env var names.")
@click.option("--host", default="localhost", show_default=True,
              help="Filter port assignments to this host.")
@click.option("--docker-compose-bin", "docker_compose_bin", default=None,
              help="Override the docker compose binary (default: 'docker compose').")
def compose_cmd(slug, compose_args, env_only, prefix, upper, host, docker_compose_bin):
    """Run docker compose for a project with port env vars auto-injected.

    Looks up the project's active port assignments on the given host and
    exposes each as an environment variable named <PURPOSE>_PORT (single)
    or <PURPOSE>_PORT_START / <PURPOSE>_PORT_END (range). Then either
    prints those (--env) or execs docker compose with them in env.

    Examples:
      devbrain compose 50tel-pbx up -d
      devbrain compose 50tel-pbx logs api
      eval "$(devbrain compose 50tel-pbx --env)"

    The project's compose_project field (if set) is passed to docker compose
    via -p, so the spawned stack uses that as its project name. This lets
    multiple devs run their own copies of the same code with their own
    per-dev port assignments side by side.
    """
    import os
    import shlex
    import subprocess as sp
    from state_machine import FactoryDB
    from config import DATABASE_URL
    from port_registry import PortRegistry

    db = FactoryDB(DATABASE_URL)
    registry = PortRegistry(db, team_ranges=_team_ranges_from_config())

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, status, compose_project, root_path FROM devbrain.projects WHERE slug = %s",
            [slug],
        )
        proj = cur.fetchone()
    if not proj:
        click.echo(f"Error: no project with slug {slug!r}.", err=True)
        sys.exit(1)
    _proj_id, project_name, status, compose_project, root_path = proj

    if status == "archived":
        click.echo(
            f"Error: project '{slug}' is archived. Reactivate first:\n"
            f"  devbrain reactivate-project --slug {slug}",
            err=True,
        )
        sys.exit(1)

    assignments = registry.list_assignments(
        host=host, project_slug=slug, include_archived=False,
    )
    env_dict = _build_compose_env(assignments, prefix=prefix, upper=upper)

    if env_only:
        for name in sorted(env_dict):
            value = env_dict[name]
            click.echo(f"export {name}={shlex.quote(value)}")
        if compose_project:
            click.echo(f"export COMPOSE_PROJECT_NAME={shlex.quote(compose_project)}")
        return

    if not compose_args:
        click.echo(
            "Error: no docker compose command supplied. "
            "Try `devbrain compose <slug> up`, `... logs api`, etc.\n"
            "Or use `--env` to print env vars without executing.",
            err=True,
        )
        sys.exit(1)

    env = {**os.environ, **env_dict}
    if compose_project:
        env["COMPOSE_PROJECT_NAME"] = compose_project

    # Default to `docker compose` (v2 plugin form). Operator can override.
    if docker_compose_bin:
        argv = shlex.split(docker_compose_bin) + list(compose_args)
    else:
        argv = ["docker", "compose"] + list(compose_args)

    if compose_project:
        argv = argv[:2] + ["-p", compose_project] + argv[2:]

    cwd = root_path if root_path else None

    click.echo(f"→ {' '.join(shlex.quote(a) for a in argv)}", err=True)
    if cwd:
        click.echo(f"  cwd: {cwd}", err=True)
    click.echo(f"  env: {len(env_dict)} port var(s) injected", err=True)

    try:
        result = sp.run(argv, env=env, cwd=cwd)
    except FileNotFoundError:
        click.echo(
            f"Error: docker compose binary not found ({argv[0]}). "
            f"Install Docker Desktop or pass --docker-compose-bin.",
            err=True,
        )
        sys.exit(1)
    sys.exit(result.returncode)


@click.command(name="reclaim-port")
@click.option("--host", default="localhost", show_default=True)
@click.option("--port", "port_spec", required=True, help="Port or range, e.g. 18000 or 20000-20100")
@click.option("--for-project", "project_slug", required=True, help="New owner project slug")
@click.confirmation_option(prompt="Reclaim this port range from its archived owner?")
def reclaim_port_cmd(host, port_spec, project_slug):
    """Transfer an archived port range to a new project (requires confirmation)."""
    from state_machine import FactoryDB
    from config import DATABASE_URL
    from port_registry import PortRegistry, parse_port_spec

    db = FactoryDB(DATABASE_URL)
    registry = PortRegistry(db, team_ranges=_team_ranges_from_config())

    port_range = parse_port_spec(port_spec)

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.projects WHERE slug = %s",
            [project_slug],
        )
        row = cur.fetchone()
        if not row:
            click.echo(f"Error: no project with slug {project_slug!r}", err=True)
            sys.exit(1)
        new_project_id = row[0]

    registry.reclaim(host=host, port_range=port_range, new_project_id=str(new_project_id))
    click.echo(f"✅ Reclaimed {host}:{port_spec} for project '{project_slug}'.")


@click.command(name="seed-ports")
@click.option(
    "--from",
    "yaml_path",
    type=click.Path(exists=True, dir_okay=False, resolve_path=True),
    default=str(Path.home() / "dev-port-registry.yml"),
    show_default=True,
    help="Path to dev-port-registry.yml",
)
@click.option("--host", default="localhost", show_default=True)
@click.option("--dry-run", is_flag=True, help="Read + report; don't write")
def seed_ports_cmd(yaml_path, host, dry_run):
    """One-time import of an existing YAML port registry into DevBrain."""
    from state_machine import FactoryDB
    from config import DATABASE_URL
    from seed_ports import import_registry

    db = FactoryDB(DATABASE_URL)
    summary = import_registry(db, Path(yaml_path), host=host, dry_run=dry_run)

    label = "[dry-run] " if dry_run else ""
    click.echo(f"{label}projects: {summary['projects_created']} created, {summary['projects_existing']} existing")
    click.echo(f"{label}ports:    {summary['ports_created']} created, {summary['ports_existing']} existing")
    if summary["skipped"]:
        click.echo(f"{label}skipped ({len(summary['skipped'])}):")
        for msg in summary["skipped"]:
            click.echo(f"  • {msg}")


def register(cli_group: click.Group) -> None:
    """Wire all project-CLI commands onto the parent click group."""
    cli_group.add_command(create_project_cmd)
    cli_group.add_command(archive_project_cmd)
    cli_group.add_command(reactivate_project_cmd)
    cli_group.add_command(ports_cmd)
    cli_group.add_command(assign_port_cmd)
    cli_group.add_command(unassign_port_cmd)
    cli_group.add_command(reclaim_port_cmd)
    cli_group.add_command(compose_cmd)
    cli_group.add_command(seed_ports_cmd)
