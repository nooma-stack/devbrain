#!/bin/bash
# Launch the DevBrain MCP server.
# Resolves DEVBRAIN_HOME from the script location so this works on any install.
DEVBRAIN_HOME="${DEVBRAIN_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
exec node "$DEVBRAIN_HOME/mcp-server/dist/index.js"
