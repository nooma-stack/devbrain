"""Stdin/stdout entry point for the MCP server's end_session enrichment.

The MCP server (TypeScript) shells out to this module via:

    python -m curator.end_session_entry

passing JSON on stdin:

    {
      "project_id": "<uuid>",
      "session_id": "<string>",
      "cascade_decisions": [...],
      "new_relationships": [...],
      "lesson_candidates": [...]
    }

and reads JSON from stdout (the result dict from
``end_session_idempotent_handler``).

On error the entry point exits non-zero with the traceback on stderr;
the MCP server surfaces that to the caller as a tool error so the agent
can correct and retry.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from uuid import UUID

# When invoked as `python -m curator.end_session_entry` from outside the
# factory/ tree, sys.path may not include factory/. The MCP server's
# spawnSync sets cwd=DEVBRAIN_REPO_ROOT, so factory/ isn't a sibling on
# the import path by default. Insert it explicitly so the
# `from curator.end_session ...` import resolves.
_FACTORY = Path(__file__).resolve().parents[1]
if str(_FACTORY) not in sys.path:
    sys.path.insert(0, str(_FACTORY))


def main() -> int:
    payload = json.load(sys.stdin)

    project_id_raw = payload.pop("project_id", None)
    if not project_id_raw:
        print("project_id required", file=sys.stderr)
        return 2
    project_id = UUID(project_id_raw)

    # Local imports so a stdin parse failure above doesn't pull in psycopg2.
    import psycopg2
    import psycopg2.extras

    from config import DATABASE_URL  # noqa: E402
    from curator.end_session import (  # noqa: E402
        end_session_idempotent_handler,
    )

    psycopg2.extras.register_uuid()
    conn = psycopg2.connect(DATABASE_URL)
    try:
        result = end_session_idempotent_handler(conn, project_id, payload)
    finally:
        conn.close()

    json.dump(result, sys.stdout, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
