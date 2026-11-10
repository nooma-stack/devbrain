#!/usr/bin/env python3
"""Merge duplicate decisions, patterns and issues.

The August 2026 re-extraction loop (and repeat extraction generally) left
many reworded copies of the same memory, so a search could return three
versions of one decision. This keeps one copy of each and archives the rest,
each linked from the kept copy by a ``supersedes`` edge
(created_by = ``merge_duplicates``), so the merge is recorded and reversible.

A pair is a duplicate when both rows are active, in the same project and
kind, at least ``--threshold`` cosine-similar (default 0.95), and either
  * extracted from the same session (same provenance_id), or
  * share the same title (case- and space-insensitive).

Grouping is greedy around a kept copy: a row is merged only into a kept
copy it is itself similar to, never through a chain (A~B, B~C does not
merge A and C). Rows with any dependency edge are never archived; they can
be the kept copy. Kept copy preference: has edges, most hits, longest
content, oldest.

Usage (from ingest/):
    python merge_duplicates.py                 # dry run: counts + samples
    python merge_duplicates.py --apply
    python merge_duplicates.py --undo --apply  # restore everything it merged
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

CREATED_BY = "merge_duplicates"
ATOM_KINDS = ("decision", "pattern", "issue")


@dataclass(frozen=True)
class Node:
    id: str
    kind: str
    project: str
    title: str
    has_edges: bool
    hit_count: int
    content_len: int
    created_at: datetime


@dataclass(frozen=True)
class Merge:
    keep: str
    drop: str
    sim: float
    reason: str


def _priority(n: Node) -> tuple:
    return (not n.has_edges, -n.hit_count, -n.content_len, n.created_at, n.id)


def plan_merges(pairs: list[tuple[str, str, float, str]], nodes: dict[str, Node]) -> list[Merge]:
    """Greedy leader grouping over similar pairs. Pure; no DB."""
    adj: dict[str, dict[str, tuple[float, str]]] = defaultdict(dict)
    for a, b, sim, reason in pairs:
        for x, y in ((a, b), (b, a)):
            prev = adj[x].get(y)
            if prev is None or sim > prev[0]:
                adj[x][y] = (sim, reason)

    assigned: set[str] = set()
    merges: list[Merge] = []
    for node in sorted((nodes[i] for i in adj if i in nodes), key=_priority):
        if node.id in assigned:
            continue
        assigned.add(node.id)
        for other_id, (sim, reason) in sorted(adj[node.id].items(), key=lambda kv: -kv[1][0]):
            other = nodes.get(other_id)
            if other is None or other_id in assigned or other.has_edges:
                continue
            assigned.add(other_id)
            merges.append(Merge(node.id, other_id, sim, reason))
    return merges


# ── database ────────────────────────────────────────────────────────────────

_PAIR_SQL = """
SELECT a.id::text, b.id::text, 1 - (a.embedding <=> b.embedding) AS sim
FROM devbrain.memory a
JOIN devbrain.memory b
  ON a.project_id = b.project_id AND a.kind = b.kind AND a.id < b.id AND {join}
WHERE a.archived_at IS NULL AND b.archived_at IS NULL
  AND a.kind = ANY(%(kinds)s) AND b.kind = ANY(%(kinds)s)
  AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
  AND 1 - (a.embedding <=> b.embedding) >= %(threshold)s
"""

_JOINS = {
    "same session": "a.provenance_id = b.provenance_id",
    "same title": "lower(trim(a.title)) = lower(trim(b.title))",
}

_NODE_SQL = """
SELECT m.id::text, m.kind, p.slug, coalesce(m.title, ''),
       EXISTS (SELECT 1 FROM devbrain.memory_dependencies d
               WHERE d.from_memory_id = m.id OR d.to_memory_id = m.id),
       coalesce(m.hit_count, 0), length(m.content), m.created_at
FROM devbrain.memory m JOIN devbrain.projects p ON p.id = m.project_id
WHERE m.id = ANY(%(ids)s::uuid[])
"""


def fetch_pairs(cur, threshold: float) -> list[tuple[str, str, float, str]]:
    pairs = []
    for reason, join in _JOINS.items():
        cur.execute(_PAIR_SQL.format(join=join), {"kinds": list(ATOM_KINDS), "threshold": threshold})
        pairs += [(a, b, float(sim), reason) for a, b, sim in cur.fetchall()]
    return pairs


def fetch_nodes(cur, ids: set[str]) -> dict[str, Node]:
    cur.execute(_NODE_SQL, {"ids": list(ids)})
    return {r[0]: Node(*r) for r in cur.fetchall()}


def apply_merges(cur, merges: list[Merge], threshold: float) -> int:
    cur.execute(
        "CREATE TEMP TABLE merge_plan (keep uuid, drop uuid, sim float8, reason text) ON COMMIT DROP"
    )
    cur.executemany(
        "INSERT INTO merge_plan VALUES (%s, %s, %s, %s)",
        [(m.keep, m.drop, min(max(m.sim, 0.0), 1.0), m.reason) for m in merges],
    )
    cur.execute(
        "UPDATE devbrain.memory m SET archived_at = now() FROM merge_plan p "
        "WHERE m.id = p.drop AND m.archived_at IS NULL"
    )
    archived = cur.rowcount
    cur.execute(
        "INSERT INTO devbrain.memory_dependencies "
        "  (from_memory_id, to_memory_id, edge_type, confidence, created_by, metadata) "
        "SELECT keep, drop, 'supersedes', sim, %s, "
        "       jsonb_build_object('reason', reason, 'threshold', %s::float8) "
        "FROM merge_plan ON CONFLICT DO NOTHING",
        (CREATED_BY, threshold),
    )
    return archived


def undo(cur) -> tuple[int, int]:
    cur.execute(
        "UPDATE devbrain.memory m SET archived_at = NULL "
        "FROM devbrain.memory_dependencies d "
        "WHERE d.created_by = %s AND d.edge_type = 'supersedes' "
        "  AND d.to_memory_id = m.id AND m.archived_at IS NOT NULL",
        (CREATED_BY,),
    )
    restored = cur.rowcount
    cur.execute("DELETE FROM devbrain.memory_dependencies WHERE created_by = %s", (CREATED_BY,))
    return restored, cur.rowcount


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--threshold", type=float, default=0.95)
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--undo", action="store_true", help="restore every row this tool merged")
    ap.add_argument("--samples", type=int, default=5)
    args = ap.parse_args()

    from db import get_connection  # noqa: PLC0415  (ingest/db.py)

    with get_connection() as conn, conn.cursor() as cur:
        if args.undo:
            restored, edges = undo(cur)
            print(f"restored {restored} rows, removed {edges} merge edges")
        else:
            pairs = fetch_pairs(cur, args.threshold)
            ids = {a for a, *_ in pairs} | {b for _, b, *_ in pairs}
            nodes = fetch_nodes(cur, ids)
            merges = plan_merges(pairs, nodes)
            by = defaultdict(int)
            for m in merges:
                n = nodes[m.drop]
                by[(n.project, n.kind, m.reason)] += 1
            print(f"pairs >= {args.threshold}: {len(pairs)}; rows to archive: {len(merges)}; "
                  f"kept copies: {len({m.keep for m in merges})}")
            for (project, kind, reason), n in sorted(by.items(), key=lambda kv: -kv[1])[:15]:
                print(f"  {project:24} {kind:9} {reason:12} {n}")
            for m in merges[: args.samples]:
                print(f"  [{m.sim:.3f}] keep: {nodes[m.keep].title[:70]}\n"
                      f"          drop: {nodes[m.drop].title[:70]}")
            if args.apply:
                print(f"archived {apply_merges(cur, merges, args.threshold)}")
        if args.apply:
            conn.commit()
            print("applied")
        else:
            conn.rollback()
            print("dry run: nothing written (pass --apply)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
