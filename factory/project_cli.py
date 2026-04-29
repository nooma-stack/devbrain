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
    cli_group.add_command(reclaim_port_cmd)
    cli_group.add_command(seed_ports_cmd)
