#!/bin/bash
# Install, render, dry-run, or uninstall the DevBrain resilience service.
#
# The Python module owns validation and service-manager operations. Unknown
# flags are intentionally rejected by argparse; this wrapper does not silently
# discard installer options.
#
# Examples:
#   bash scripts/install-resilience.sh --profile workstation
#   bash scripts/install-resilience.sh --profile studio --dry-run
#   bash scripts/install-resilience.sh --with-heartbeat \
#       --heartbeat-url https://health.example.invalid/ping
#   bash scripts/install-resilience.sh --restart
#   bash scripts/install-resilience.sh --uninstall --yes

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEVBRAIN_HOME="${DEVBRAIN_HOME:-$(cd "$SCRIPT_DIR/.." && pwd)}"

if [[ -n "${DEVBRAIN_PYTHON:-}" ]]; then
    PYTHON="$DEVBRAIN_PYTHON"
elif [[ -x "$DEVBRAIN_HOME/.venv/bin/python" ]]; then
    PYTHON="$DEVBRAIN_HOME/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
else
    echo "Error: Python 3 is required (set DEVBRAIN_PYTHON to its path)." >&2
    exit 1
fi

export DEVBRAIN_HOME
cd "$DEVBRAIN_HOME"
exec "$PYTHON" -m ops.resilience.install "$@"
