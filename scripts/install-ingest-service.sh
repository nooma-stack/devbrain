#!/bin/bash
# Install the DevBrain ingest service as a launchd agent (macOS only).
#
# Generates ~/Library/LaunchAgents/com.devbrain.ingest.plist from
# com.devbrain.ingest.plist.template, substituting @DEVBRAIN_HOME@ with
# the resolved path to this repository, then loads the service.
#
# Linux/Windows: see docs/INSTALL.md for systemd / manual alternatives.

set -euo pipefail

if [[ "$OSTYPE" != darwin* ]]; then
    echo "Error: this installer is macOS-only (uses launchd)." >&2
    echo "Linux: use the systemd unit at scripts/devbrain-ingest.service" >&2
    exit 1
fi

DEVBRAIN_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$DEVBRAIN_HOME/com.devbrain.ingest.plist.template"
TARGET="$HOME/Library/LaunchAgents/com.devbrain.ingest.plist"

if [[ ! -f "$TEMPLATE" ]]; then
    echo "Error: template not found at $TEMPLATE" >&2
    exit 1
fi

if [[ ! -x "$DEVBRAIN_HOME/ingest/.venv/bin/python" ]]; then
    echo "Error: ingest venv not found at $DEVBRAIN_HOME/ingest/.venv" >&2
    echo "Run: cd ingest && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$DEVBRAIN_HOME/logs"

# Substitute placeholder and write to LaunchAgents
sed "s|@DEVBRAIN_HOME@|$DEVBRAIN_HOME|g" "$TEMPLATE" > "$TARGET"
echo "Wrote: $TARGET"

# Unload if previously loaded, then load fresh
launchctl unload "$TARGET" 2>/dev/null || true
launchctl load "$TARGET"

echo "Loaded launchd service: com.devbrain.ingest"
echo "Logs: $DEVBRAIN_HOME/logs/ingest.log"
echo
echo "To uninstall: launchctl unload $TARGET && rm $TARGET"
