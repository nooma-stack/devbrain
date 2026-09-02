#!/bin/bash
# Steady-state closure scanner wrapper (hourly launchd).
# Credentials: sources ~/.config/devbrain/closure.env if present
# (OPENAI_API_KEY=... , DEVBRAIN_CLOSURE_REMOTE_OK=1); otherwise, if
# GOOGLE_APPLICATION_CREDENTIALS points at a service account with access,
# fetches the key from GCP Secret Manager (the LHT pattern).
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$HOME/.config/devbrain/closure.env" ] && { set -a; . "$HOME/.config/devbrain/closure.env"; set +a; }
if [ -z "${OPENAI_API_KEY:-}" ] && [ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
  OPENAI_API_KEY=$(gcloud secrets versions access latest --secret=openai-api-key \
    --project="${DEVBRAIN_GCP_PROJECT:-brightbot-478117}" 2>/dev/null || true)
  export OPENAI_API_KEY
fi
exec "$DIR/../.venv/bin/python" "$DIR/close_orphan_sessions.py" \
  --backend "${CLOSURE_BACKEND:-openai}" --limit "${CLOSURE_LIMIT:-5}" \
  --settle-hours "${CLOSURE_SETTLE_HOURS:-6}"
