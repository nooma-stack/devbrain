"""CLI entry point for the deep_search with_graph extension (Phase 5d).

Reads JSON from stdin with a list of seed memory_ids (from deep_search's
top results) and walker parameters. Runs the walker from each seed,
deduplicates memories across all walker results, and returns the combined
graph neighborhood.

Input JSON schema:
    {
        "seed_memory_ids": ["<uuid>", ...],
        "graph_max_hops":  3,
        "graph_max_nodes": 50
    }

    Note: edge_types and direction use walker defaults (strong-signal,
    both). These are intentionally not exposed on deep_search per the
    design doc §5.2. cross_project defaults to False (same-project as
    deep_search).

Output JSON schema:
    {
        "memories": [
            {
                "id": "<uuid>",
                "project_id": "<uuid>",
                "kind": "decision",
                "title": "...",
                "content": "...",
                "strength": 1.0,
                "hops": 0
            }, ...
        ],
        "edges": [
            {
                "from_memory_id": "<uuid>",
                "to_memory_id": "<uuid>",
                "edge_type": "depends_on",
                "confidence": 1.0
            }, ...
        ],
        "seeds": ["<uuid>", ...]
    }

On error, writes {"error": "<message>"} and exits 1.
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import psycopg2
import psycopg2.extras

from config import build_database_url
from graph.walker import walk, STRONG_SIGNAL_EDGE_TYPES


def _uuid_str(val) -> str | None:
    if val is None:
        return None
    return str(val)


def main() -> None:
    psycopg2.extras.register_uuid()

    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"error": f"Invalid JSON input: {exc}"}))
        sys.exit(1)

    seed_ids = payload.get("seed_memory_ids", [])
    if not seed_ids:
        # No seeds → empty graph (not an error)
        print(json.dumps({"memories": [], "edges": [], "seeds": []}))
        return

    graph_max_hops = int(payload.get("graph_max_hops", 3))
    graph_max_nodes = int(payload.get("graph_max_nodes", 50))

    try:
        database_url = build_database_url()
        conn = psycopg2.connect(database_url)
    except Exception as exc:
        print(json.dumps({"error": f"DB connection failed: {exc}"}))
        sys.exit(1)

    # Walk from each seed, deduplicating memories across results.
    # We collect all visited memory IDs and take the minimum hop
    # distance when the same memory is reachable from multiple seeds.
    all_memories: dict[str, dict] = {}  # id -> best memory dict
    all_edge_set: set[tuple] = set()    # (from, to, type) for dedup
    valid_seeds: list[str] = []

    try:
        for seed in seed_ids:
            try:
                result = walk(
                    conn,
                    seed,
                    edge_types=STRONG_SIGNAL_EDGE_TYPES,
                    max_hops=graph_max_hops,
                    max_nodes=graph_max_nodes,
                    direction="both",
                    cross_project=False,
                )
            except Exception:
                # Non-existent or archived seed — skip silently
                continue

            if not result.memories:
                continue

            valid_seeds.append(seed)

            for m in result.memories:
                mid = str(m.id)
                existing = all_memories.get(mid)
                if existing is None or m.hops < existing["hops"]:
                    all_memories[mid] = {
                        "id": _uuid_str(m.id),
                        "project_id": _uuid_str(m.project_id),
                        "kind": m.kind,
                        "title": m.title,
                        "content": m.content,
                        "strength": m.strength,
                        "hops": m.hops,
                    }

            for e in result.edges:
                key = (_uuid_str(e.from_memory_id), _uuid_str(e.to_memory_id), e.edge_type)
                if key not in all_edge_set:
                    all_edge_set.add(key)

    finally:
        conn.close()

    # Sort memories by (hops asc, strength desc)
    memories_list = sorted(
        all_memories.values(),
        key=lambda m: (m["hops"], -m["strength"]),
    )

    edges_list = [
        {"from_memory_id": k[0], "to_memory_id": k[1], "edge_type": k[2]}
        for k in sorted(all_edge_set)
    ]

    output = {
        "memories": memories_list,
        "edges": edges_list,
        "seeds": valid_seeds,
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()
