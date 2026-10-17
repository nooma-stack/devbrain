#!/bin/bash
# Install the DevBrain local reranker as a launchd agent (macOS, Apple silicon).
#
#   rerank/install.sh            # venv + model download + launchd service
#   rerank/install.sh --uninstall
#
# Env overrides: DEVBRAIN_RERANK_MODEL, DEVBRAIN_RERANK_REVISION (pin a
# model commit), DEVBRAIN_RERANK_PORT (default 18600).
#
# Steps: create rerank/.venv (Python 3.12 via uv, or python3.12), install
# rerank/requirements.txt, download the model once (the only network use;
# the service itself runs with HF_HUB_OFFLINE=1), write
# ~/Library/LaunchAgents/com.devbrain.rerank.plist from the template, load
# it, and wait for /health. Re-running is safe (it reinstalls in place).

set -euo pipefail

if [[ "$OSTYPE" != darwin* ]]; then
    echo "Error: this installer is macOS-only (uses launchd)." >&2
    exit 1
fi

RERANK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEVBRAIN_HOME="$(cd "$RERANK_DIR/.." && pwd)"
LABEL="com.devbrain.rerank"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
DEFAULT_MODEL="Alibaba-NLP/gte-reranker-modernbert-base"
# Pinned so every Studio scores with the same weights (the commit audited on
# 2026-10-17). Override DEVBRAIN_RERANK_REVISION to move it.
DEFAULT_REVISION="f7481e6055501a30fb19d090657df9ec1f79ab2c"
MODEL="${DEVBRAIN_RERANK_MODEL:-$DEFAULT_MODEL}"
if [[ -n "${DEVBRAIN_RERANK_REVISION+x}" ]]; then
    REVISION="$DEVBRAIN_RERANK_REVISION"
elif [[ "$MODEL" == "$DEFAULT_MODEL" ]]; then
    REVISION="$DEFAULT_REVISION"
else
    REVISION=""
fi
PORT="${DEVBRAIN_RERANK_PORT:-18600}"
VENV="$RERANK_DIR/.venv"

if [[ "${1:-}" == "--uninstall" ]]; then
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$TARGET"
    echo "Removed $LABEL (venv and model cache left in place: $VENV, ~/.cache/huggingface)."
    exit 0
fi

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | grep -qv "^COMMAND"; then
    holder="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | awk 'NR==2 {print $1" pid "$2}')"
    if ! launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
        echo "Error: port $PORT is already in use by $holder. Set DEVBRAIN_RERANK_PORT." >&2
        exit 1
    fi
fi

echo "==> venv: $VENV"
if command -v uv >/dev/null 2>&1; then
    [[ -x "$VENV/bin/python" ]] || uv venv --python 3.12 "$VENV" -q
    uv pip install --python "$VENV/bin/python" -q -r "$RERANK_DIR/requirements.txt"
else
    PY="$(command -v python3.12 || true)"
    [[ -n "$PY" ]] || { echo "Error: need uv or python3.12" >&2; exit 1; }
    [[ -x "$VENV/bin/python" ]] || "$PY" -m venv "$VENV"
    "$VENV/bin/pip" install -q -r "$RERANK_DIR/requirements.txt"
fi

echo "==> model: $MODEL ${REVISION:+@ $REVISION}"
DEVBRAIN_RERANK_MODEL="$MODEL" DEVBRAIN_RERANK_REVISION="$REVISION" "$VENV/bin/python" - <<'PY'
import os
from huggingface_hub import snapshot_download
path = snapshot_download(
    os.environ["DEVBRAIN_RERANK_MODEL"],
    revision=os.environ.get("DEVBRAIN_RERANK_REVISION") or None,
    # PyTorch weights + configs only: skip ONNX / OpenVINO / other exports.
    ignore_patterns=["onnx/*", "openvino/*", "*.onnx", "*.h5", "*.msgpack", "*.ot", "*.gguf"],
)
print("    cached at", path)
PY

mkdir -p "$HOME/Library/LaunchAgents" "$DEVBRAIN_HOME/logs"
sed -e "s|@DEVBRAIN_HOME@|$DEVBRAIN_HOME|g" -e "s|@MODEL@|$MODEL|g" \
    -e "s|@REVISION@|$REVISION|g" -e "s|@PORT@|$PORT|g" \
    "$RERANK_DIR/com.devbrain.rerank.plist.template" > "$TARGET"
echo "==> wrote $TARGET"

DOMAIN="gui/$(id -u)"
if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    # bootout is asynchronous; bootstrapping before it finishes fails with EIO.
    for _ in $(seq 1 40); do
        launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
        sleep 0.5
    done
fi
launchctl bootstrap "$DOMAIN" "$TARGET"

echo -n "==> waiting for http://127.0.0.1:$PORT/health "
for _ in $(seq 1 90); do
    if out="$(curl -sf "http://127.0.0.1:$PORT/health" 2>/dev/null)"; then
        echo; echo "    $out"; echo "Logs: $DEVBRAIN_HOME/logs/rerank.err.log"
        exit 0
    fi
    echo -n "."; sleep 2
done
echo; echo "Error: service did not become healthy; see $DEVBRAIN_HOME/logs/rerank.err.log" >&2
exit 1
