"""One-time importer for ~/dev-port-registry.yml → devbrain.projects + port_assignments.

The pre-DevBrain port registry was a hand-edited YAML file at
`~/dev-port-registry.yml` of the form:

    projects:
      <slug>:
        team: <org>
        status: active|inactive|archived|experimental
        path: <abs path>
        compose_project: <docker-compose project name>
        ports:
          <purpose>: <port>          # single port: 18000
          <purpose>: <start>-<end>   # range: "20000-20100"

This module reads that file and creates corresponding rows in
devbrain.projects + devbrain.port_assignments. Idempotent: re-running on
the same YAML preserves existing rows and adds only new ones.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

import yaml

from port_registry import PortRange, parse_port_spec

logger = logging.getLogger(__name__)


def parse_registry(yaml_text: str) -> dict[str, Any]:
    """Parse the YAML registry into a dict, return empty if malformed/empty."""
    data = yaml.safe_load(yaml_text) or {}
    if not isinstance(data, dict):
        return {}
    return data.get("projects", {}) or {}


def import_registry(
    db,
    yaml_path: Path,
    host: str = "localhost",
    dry_run: bool = False,
) -> dict:
    """Read yaml_path, ensure each project + its ports exist in DevBrain.

    Returns a summary dict:
        {
            "projects_created": int,
            "projects_existing": int,
            "ports_created": int,
            "ports_existing": int,
            "skipped": [list of error messages],
        }
    """
    text = yaml_path.read_text(encoding="utf-8")
    registry = parse_registry(text)

    summary = {
        "projects_created": 0,
        "projects_existing": 0,
        "ports_created": 0,
        "ports_existing": 0,
        "skipped": [],
    }

    for slug, cfg in registry.items():
        if not isinstance(cfg, dict):
            summary["skipped"].append(f"{slug}: config is not a mapping")
            continue
        try:
            _import_project(db, slug, cfg, host, dry_run, summary)
        except Exception as e:
            summary["skipped"].append(f"{slug}: {type(e).__name__}: {e}")

    return summary


def _import_project(
    db,
    slug: str,
    cfg: dict,
    default_host: str,
    dry_run: bool,
    summary: dict,
) -> None:
    name = cfg.get("name") or slug
    team = cfg.get("team")
    status = (cfg.get("status") or "active").strip().lower()
    if status not in {"active", "inactive", "archived", "experimental"}:
        summary["skipped"].append(f"{slug}: unknown status {status!r}")
        return
    path = cfg.get("path")
    compose_project = cfg.get("compose_project")
    ports = cfg.get("ports") or {}

    project_id = _ensure_project_row(
        db,
        slug=slug,
        name=name,
        team=team,
        status=status,
        path=path,
        compose_project=compose_project,
        dry_run=dry_run,
        summary=summary,
    )
    if project_id is None and not dry_run:
        return

    for purpose, spec in ports.items():
        try:
            port_range = parse_port_spec(str(spec))
        except (ValueError, TypeError) as e:
            summary["skipped"].append(f"{slug}/{purpose}: bad port spec {spec!r} ({e})")
            continue
        _ensure_port_row(
            db,
            project_id=project_id,
            host=default_host,
            purpose=str(purpose),
            port_range=port_range,
            dry_run=dry_run,
            summary=summary,
        )


def _ensure_project_row(
    db,
    *,
    slug: str,
    name: str,
    team: Optional[str],
    status: str,
    path: Optional[str],
    compose_project: Optional[str],
    dry_run: bool,
    summary: dict,
) -> Optional[str]:
    if dry_run:
        summary["projects_created"] += 1
        return None
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.projects WHERE slug = %s",
            [slug],
        )
        row = cur.fetchone()
        if row:
            summary["projects_existing"] += 1
            cur.execute(
                """
                UPDATE devbrain.projects
                SET name = COALESCE(%s, name),
                    team = COALESCE(%s, team),
                    status = COALESCE(%s, status),
                    root_path = COALESCE(%s, root_path),
                    compose_project = COALESCE(%s, compose_project),
                    updated_at = now()
                WHERE id = %s
                """,
                [name, team, status, path, compose_project, row[0]],
            )
            conn.commit()
            return str(row[0])
        cur.execute(
            """
            INSERT INTO devbrain.projects
                (slug, name, team, status, root_path, compose_project)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            [slug, name, team, status, path, compose_project],
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        summary["projects_created"] += 1
        return str(new_id)


def _ensure_port_row(
    db,
    *,
    project_id: Optional[str],
    host: str,
    purpose: str,
    port_range: PortRange,
    dry_run: bool,
    summary: dict,
) -> None:
    if dry_run or project_id is None:
        summary["ports_created"] += 1
        return
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, port_start, port_end FROM devbrain.port_assignments
            WHERE project_id = %s AND purpose = %s
            """,
            [project_id, purpose],
        )
        row = cur.fetchone()
        if row:
            existing_id, e_start, e_end = row
            if e_start == port_range.start and e_end == port_range.end:
                summary["ports_existing"] += 1
                return
            cur.execute(
                """
                UPDATE devbrain.port_assignments
                SET port_start = %s, port_end = %s, host = %s
                WHERE id = %s
                """,
                [port_range.start, port_range.end, host, existing_id],
            )
            conn.commit()
            summary["ports_existing"] += 1
            return
        cur.execute(
            """
            INSERT INTO devbrain.port_assignments
                (project_id, host, purpose, port_start, port_end)
            VALUES (%s, %s, %s, %s, %s)
            """,
            [project_id, host, purpose, port_range.start, port_range.end],
        )
        conn.commit()
        summary["ports_created"] += 1
