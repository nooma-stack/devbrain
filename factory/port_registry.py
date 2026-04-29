"""Project port registry — allocator and assignment management.

Replaces the manual ~/dev-port-registry.yml workflow with a queryable
DB-backed registry. The factory + AI agents call into this module to
suggest free ports (respecting team ranges + already-reserved ports for
inactive projects) and to record assignments atomically with project
creation.

Design references:
- /Users/patrickkelly/Nooma-Stack/50Tel PBX/docs/local-dev-port-registry.md
  (canonical policy doc)
- /Users/patrickkelly/dev-port-registry.yml (existing YAML registry, seeded via
  `devbrain seed-ports`)

Key invariants:
- Ports are RESERVED even when project.status = 'inactive'. Only when
  the project is explicitly `archived` and a human approves can another
  project reclaim those ports (via `reclaim_port`).
- Port assignments support ranges (port_start <= port_end). A single
  port has port_start == port_end.
- Overlap detection is per-host: the same port can be in use on
  `localhost` and on `lht-vps` simultaneously without conflict.
- Allocator suggestions never silently propose archived ports — they
  surface them as `needs_approval` candidates that the caller decides on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PortRange:
    """A contiguous port range [start, end] inclusive. Single port: start == end."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if not (1 <= self.start <= self.end <= 65535):
            raise ValueError(
                f"invalid port range {self.start}-{self.end}: must satisfy 1 <= start <= end <= 65535"
            )

    @property
    def size(self) -> int:
        return self.end - self.start + 1

    def overlaps(self, other: "PortRange") -> bool:
        return not (self.end < other.start or self.start > other.end)


@dataclass(frozen=True)
class TeamRange:
    """A team's reserved port range for a category (web, apis, db_cache, etc.)."""

    team: str
    category: str
    range: PortRange


@dataclass(frozen=True)
class PortAssignment:
    """A live port assignment record from the DB, normalized."""

    project_id: str
    project_slug: str
    project_status: str  # active | inactive | archived | experimental
    host: str
    purpose: str
    port_range: PortRange
    archived_at: Optional[str] = None
    notes: Optional[str] = None


@dataclass(frozen=True)
class Suggestion:
    """A port allocation suggestion from suggest_ports."""

    purpose: str
    host: str
    range: PortRange
    needs_approval: bool = False
    reclaim_from_project: Optional[str] = None  # slug of archived project, if reclaiming


def parse_port_spec(spec: str) -> PortRange:
    """Parse a port spec string into a PortRange.

    Accepts:
    - Single port: "8000" → PortRange(8000, 8000)
    - Range: "20000-20100" → PortRange(20000, 20100)
    """
    spec = spec.strip()
    if "-" in spec:
        start_s, end_s = spec.split("-", 1)
        return PortRange(int(start_s.strip()), int(end_s.strip()))
    p = int(spec)
    return PortRange(p, p)


def format_port_range(r: PortRange) -> str:
    """Reverse of parse_port_spec — produces "8000" or "20000-20100"."""
    return str(r.start) if r.start == r.end else f"{r.start}-{r.end}"


def find_first_free_range(
    base: int,
    size: int,
    occupied: list[PortRange],
    cap: int = 65535,
) -> Optional[PortRange]:
    """Return the first free contiguous range of the given size starting at >= base.

    `occupied` is the list of already-taken ranges on the same host.
    Returns None if no free range fits before `cap`.
    """
    if size < 1:
        raise ValueError("size must be >= 1")

    # Sort occupied ranges by start
    sorted_occ = sorted(occupied, key=lambda r: r.start)

    candidate_start = base
    for r in sorted_occ:
        if r.end < candidate_start:
            continue  # behind us
        if candidate_start + size - 1 < r.start:
            # Free gap of `size` exists before this occupied range
            return PortRange(candidate_start, candidate_start + size - 1)
        # Overlap or adjacency — skip past this range
        candidate_start = max(candidate_start, r.end + 1)

    if candidate_start + size - 1 <= cap:
        return PortRange(candidate_start, candidate_start + size - 1)
    return None


def default_team_base(team: Optional[str], category: str, team_ranges: dict) -> int:
    """Return the recommended base port for a (team, category) combination.

    `team_ranges` is the parsed config dict, e.g.:
      { "nooma-stack": { "web": [13000, 13999], "apis": [18000, 18999], ... }, ... }

    Falls back to 3000 (Node convention) if no match. Caller is expected to
    have already mapped the assignment's purpose to a category (api, web,
    db_cache, etc.) — that mapping is left to the CLI/agent layer because
    different teams use different category naming.
    """
    if not team or not team_ranges:
        return 3000
    team_cfg = team_ranges.get(team) or team_ranges.get(team.lower())
    if not team_cfg:
        return 3000
    cat_range = team_cfg.get(category)
    if not cat_range:
        return 3000
    return int(cat_range[0])


def suggest_port_range(
    purpose: str,
    host: str,
    size: int,
    occupied_active: list[PortRange],
    occupied_archived: list[tuple[PortRange, str]],
    team: Optional[str] = None,
    category: Optional[str] = None,
    team_ranges: Optional[dict] = None,
    explicit_base: Optional[int] = None,
) -> Suggestion:
    """Suggest the next free port range for a purpose on a host.

    Resolution order:
    1. If explicit_base is set, start from there.
    2. Else if (team, category) maps to a team_ranges entry, use that base.
    3. Else default to 3000.

    First tries to fit in unoccupied space (no approval needed). If exhausted,
    looks for archived assignments whose ranges fit and surfaces one as
    needs_approval=True with reclaim_from_project set.

    `occupied_active`: ranges currently reserved (active OR inactive projects).
                      Treated as off-limits regardless of project state.
    `occupied_archived`: ranges from archived projects (PortRange + project_slug).
                         Available for reclaim with human approval.
    """
    if explicit_base is not None:
        base = explicit_base
    else:
        base = default_team_base(team, category or purpose, team_ranges or {})

    # Try clean allocation against active+inactive-blocked space
    blocked = list(occupied_active)
    # Archived ranges also block AUTO suggestions — agent must explicitly reclaim
    blocked.extend(r for r, _slug in occupied_archived)

    free = find_first_free_range(base, size, blocked)
    if free is not None:
        return Suggestion(purpose=purpose, host=host, range=free, needs_approval=False)

    # No clean space — see if reclaiming an archived range fits
    for r, slug in sorted(occupied_archived, key=lambda x: x[0].start):
        if r.size >= size and r.start >= base:
            reclaim_range = PortRange(r.start, r.start + size - 1)
            return Suggestion(
                purpose=purpose,
                host=host,
                range=reclaim_range,
                needs_approval=True,
                reclaim_from_project=slug,
            )

    raise NoFreePortError(
        f"no free port range of size {size} for {purpose} on {host} starting at {base}"
    )


class NoFreePortError(Exception):
    """Raised when no port range fits the request — even after considering archived ranges."""


# ────────────────────────────────────────────────────────────────────────────
# DB integration (FactoryDB-backed)
# ────────────────────────────────────────────────────────────────────────────


class PortRegistry:
    """High-level facade over devbrain.projects + devbrain.port_assignments.

    Tests inject a mock `db` with a get_conn() context manager. Production code
    passes a FactoryDB instance.
    """

    def __init__(self, db, team_ranges: Optional[dict] = None) -> None:
        self.db = db
        self.team_ranges = team_ranges or {}

    def list_assignments(
        self,
        host: Optional[str] = None,
        project_slug: Optional[str] = None,
        include_archived: bool = True,
    ) -> list[PortAssignment]:
        """Return port assignments, optionally filtered."""
        sql = """
            SELECT pa.project_id, p.slug, p.status, pa.host, pa.purpose,
                   pa.port_start, pa.port_end, pa.archived_at, pa.notes
            FROM devbrain.port_assignments pa
            JOIN devbrain.projects p ON pa.project_id = p.id
            WHERE 1=1
        """
        params: list = []
        if host:
            sql += " AND pa.host = %s"
            params.append(host)
        if project_slug:
            sql += " AND p.slug = %s"
            params.append(project_slug)
        if not include_archived:
            sql += " AND pa.archived_at IS NULL"
        sql += " ORDER BY pa.host, pa.port_start"

        rows = self._fetch(sql, params)
        return [
            PortAssignment(
                project_id=str(r[0]),
                project_slug=r[1],
                project_status=r[2],
                host=r[3],
                purpose=r[4],
                port_range=PortRange(r[5], r[6]),
                archived_at=r[7].isoformat() if r[7] else None,
                notes=r[8],
            )
            for r in rows
        ]

    def suggest(
        self,
        purpose: str,
        host: str = "localhost",
        size: int = 1,
        team: Optional[str] = None,
        category: Optional[str] = None,
        explicit_base: Optional[int] = None,
    ) -> Suggestion:
        """Suggest a free port range. Doesn't reserve anything."""
        all_assignments = self.list_assignments(host=host, include_archived=True)
        active = [
            a.port_range for a in all_assignments
            if a.project_status != "archived" and a.archived_at is None
        ]
        archived = [
            (a.port_range, a.project_slug)
            for a in all_assignments
            if a.project_status == "archived" or a.archived_at is not None
        ]
        return suggest_port_range(
            purpose=purpose,
            host=host,
            size=size,
            occupied_active=active,
            occupied_archived=archived,
            team=team,
            category=category,
            team_ranges=self.team_ranges,
            explicit_base=explicit_base,
        )

    def assign(
        self,
        project_id: str,
        host: str,
        purpose: str,
        port_range: PortRange,
        notes: Optional[str] = None,
    ) -> None:
        """Insert a port assignment row. Caller is responsible for conflict checks."""
        sql = """
            INSERT INTO devbrain.port_assignments
                (project_id, host, purpose, port_start, port_end, notes)
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        self._execute(
            sql,
            [project_id, host, purpose, port_range.start, port_range.end, notes],
        )

    def reclaim(self, host: str, port_range: PortRange, new_project_id: str) -> None:
        """Move an archived port assignment to a new project.

        Caller is expected to have confirmed with the human first. This is
        the only path by which an archived port range changes ownership.
        """
        # Mark the existing assignment as torn down (archived_at = now) and
        # create a new one for the new project. Done in one transaction.
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE devbrain.port_assignments
                SET archived_at = now()
                WHERE host = %s AND port_start = %s AND port_end = %s
                  AND archived_at IS NULL
                """,
                [host, port_range.start, port_range.end],
            )
            # Look up purpose from the project's prior assignment (best-effort —
            # caller can override via assign() if needed).
            cur.execute(
                """
                SELECT purpose FROM devbrain.port_assignments
                WHERE host = %s AND port_start = %s AND port_end = %s
                ORDER BY assigned_at DESC LIMIT 1
                """,
                [host, port_range.start, port_range.end],
            )
            row = cur.fetchone()
            purpose = row[0] if row else "claimed"
            cur.execute(
                """
                INSERT INTO devbrain.port_assignments
                    (project_id, host, purpose, port_start, port_end, notes)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                [new_project_id, host, purpose, port_range.start, port_range.end,
                 f"Reclaimed from prior assignment at {host}:{port_range.start}-{port_range.end}"],
            )
            conn.commit()

    # Internal helpers
    def _fetch(self, sql: str, params: list) -> list[tuple]:
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def _execute(self, sql: str, params: list) -> None:
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()
