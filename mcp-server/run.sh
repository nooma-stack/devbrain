#!/bin/bash
# Launch the DevBrain MCP server.
# Resolves DEVBRAIN_HOME from the script location so this works on any install.
DEVBRAIN_HOME="${DEVBRAIN_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Load .env so API keys and config overrides are available to the MCP
# server and any subprocesses it spawns (factory orchestrator, ingest
# scripts, spawned AI CLIs). Honors documented precedence (env > .env >
# defaults): vars already set in the caller's environment are preserved.
# See bin/devbrain for the rationale.
_load_dotenv_no_override() {
    local envfile="$1"
    [[ -f "$envfile" ]] || return 0
    local line key value
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        line="${line#"${line%%[![:space:]]*}"}"
        [[ -z "$line" || "$line" =~ ^# ]] && continue
        line="${line#export }"
        key="${line%%=*}"
        value="${line#*=}"
        [[ "$key" == "$line" ]] && continue
        key="${key%%[[:space:]]*}"
        if [[ "$value" =~ ^\".*\"$ ]]; then
            value="${value#\"}"; value="${value%\"}"
        elif [[ "$value" =~ ^\'.*\'$ ]]; then
            value="${value#\'}"; value="${value%\'}"
        fi
        [[ -n "${!key+x}" ]] && continue
        export "$key=$value"
    done < "$envfile"
}
_load_dotenv_no_override "$DEVBRAIN_HOME/.env"

exec node "$DEVBRAIN_HOME/mcp-server/dist/index.js"
