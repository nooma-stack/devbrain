"""CLI entry point for the graph_walk MCP tool.

Reads a JSON payload from stdin, invokes walker.walk(), and writes the
result to stdout as JSON. Called by the MCP server's graph_walk tool
via spawnSync — same pattern as curator.end_session_entry.

Input JSON schema:
    {
        "seed_memory_id": "<uuid>",
        "edge_types":     ["depends_on", ...] | null,
        "max_hops":       3,
        "max_nodes":      50,
        "direction":      "both" | "outgoing" | "incoming",
        "cross_project":  false
    }

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
        "truncated": false
    }

On error, writes {"error": "<message>"} and exits 1.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure factory/ is on path when invoked via `python -m graph.graph_walk_entry`
# from the factory/ directory. The MCP server runs spawnSync from DEVBRAIN_REPO_ROOT
# with cwd=factory so this resolves correctly.
sys.path.insert(0, str(Path(__file__).parent.parent))

import psycopg2
import psycopg2.extras

from config import build_database_url
from graph.walker import walk


def _uuid_str(val) -> str:
    """Convert a UUID or string to its canonical string form."""
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

    seed = payload.get("seed_memory_id")
    if not seed:
        print(json.dumps({"error": "seed_memory_id is required"}))
        sys.exit(1)

    edge_types = payload.get("edge_types") or None
    max_hops = int(payload.get("max_hops", 3))
    max_nodes = int(payload.get("max_nodes", 50))
    direction = payload.get("direction", "both")
    cross_project = bool(payload.get("cross_project", False))

    if direction not in ("both", "outgoing", "incoming"):
        print(json.dumps({"error": f"Invalid direction: {direction!r}"}))
        sys.exit(1)

    try:
        database_url = build_database_url()
        conn = psycopg2.connect(database_url)
    except Exception as exc:
        print(json.dumps({"error": f"DB connection failed: {exc}"}))
        sys.exit(1)

    try:
        result = walk(
            conn,
            seed,
            edge_types=edge_types,
            max_hops=max_hops,
            max_nodes=max_nodes,
            direction=direction,
            cross_project=cross_project,
        )
    except Exception as exc:
        print(json.dumps({"error": f"Walker failed: {exc}"}))
        conn.close()
        sys.exit(1)

    conn.close()

    output = {
        "memories": [
            {
                "id": _uuid_str(m.id),
                "project_id": _uuid_str(m.project_id),
                "kind": m.kind,
                "title": m.title,
                "content": m.content,
                "strength": m.strength,
                "hops": m.hops,
            }
            for m in result.memories
        ],
        "edges": [
            {
                "from_memory_id": _uuid_str(e.from_memory_id),
                "to_memory_id": _uuid_str(e.to_memory_id),
                "edge_type": e.edge_type,
                "confidence": e.confidence,
            }
            for e in result.edges
        ],
        "truncated": result.truncated,
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()
