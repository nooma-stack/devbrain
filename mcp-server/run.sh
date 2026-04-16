#!/bin/bash
# Launch the DevBrain MCP server.
# Resolves DEVBRAIN_HOME from the script location so this works on any install.
DEVBRAIN_HOME="${DEVBRAIN_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Load .env so API keys and config overrides are available to the MCP
# server and any subprocesses it spawns (factory orchestrator, ingest
# scripts, spawned AI CLIs). Without this, keys saved to .env wouldn't
# be in the environment of processes launched by the MCP server.
if [[ -f "$DEVBRAIN_HOME/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$DEVBRAIN_HOME/.env"
    set +a
fi

exec node "$DEVBRAIN_HOME/mcp-server/dist/index.js"
