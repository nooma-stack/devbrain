#!/usr/bin/env bash
# Phase 8 PR 3 — all-time fan-out backfill orchestrator.
#
# Spawns N parallel `devbrain cognify-fanout --shard=I/N` workers, each
# processing a deterministic stride of the discovered-sessions list, so
# the ~3,000-session all-time backfill finishes in roughly 1/N the
# wall-clock time of a single-worker run.
#
# The underlying cognify-fanout command is idempotent (migration 039's
# partial unique index collapses re-emissions), so this script is safe
# to re-run on partial completion / interruption.
#
# Usage:
#   scripts/backfill_fanout_all_time.sh [--workers N] [--model MODEL]
#                                       [--dry-run] [--since YYYY-MM-DD]
#
# Examples:
#   scripts/backfill_fanout_all_time.sh                          # 4 workers default
#   scripts/backfill_fanout_all_time.sh --workers 8              # heavier parallelism
#   scripts/backfill_fanout_all_time.sh --dry-run                # size scope only
#   scripts/backfill_fanout_all_time.sh --model claude-opus-4-7  # opus retry pass
#
# Logs land at ~/.devbrain/logs/fanout-backfill-<timestamp>-shard-N.log
# A summary JSON is written to ~/.devbrain/logs/fanout-backfill-<timestamp>.json
# after every worker exits.

set -euo pipefail

WORKERS=4
MODEL=""
SINCE=""
DRY_RUN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workers) WORKERS="$2"; shift 2 ;;
    --model)   MODEL="$2"; shift 2 ;;
    --since)   SINCE="$2"; shift 2 ;;
    --dry-run) DRY_RUN="--dry-run"; shift ;;
    -h|--help)
      sed -n '2,30p' "$0"
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

if ! [[ "$WORKERS" =~ ^[1-9][0-9]*$ ]]; then
  echo "Error: --workers must be a positive integer; got '$WORKERS'" >&2
  exit 2
fi

LOG_DIR="${HOME}/.devbrain/logs"
mkdir -p "$LOG_DIR"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
SUMMARY_JSON="${LOG_DIR}/fanout-backfill-${TIMESTAMP}.json"

# Resolve bin/devbrain — caller may invoke from anywhere.
REPO_ROOT="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
DEVBRAIN_BIN="${REPO_ROOT}/bin/devbrain"
if [[ ! -x "$DEVBRAIN_BIN" ]]; then
  echo "Error: $DEVBRAIN_BIN not executable" >&2
  exit 2
fi

echo "fanout-backfill: $WORKERS workers; logs at $LOG_DIR/fanout-backfill-${TIMESTAMP}-shard-*.log"

declare -a PIDS
declare -a SHARD_LOGS
declare -a SHARD_JSON_LOGS

for ((i=0; i<WORKERS; i++)); do
  SHARD_LOG="${LOG_DIR}/fanout-backfill-${TIMESTAMP}-shard-${i}.log"
  SHARD_JSON="${LOG_DIR}/fanout-backfill-${TIMESTAMP}-shard-${i}.json"
  SHARD_LOGS+=("$SHARD_LOG")
  SHARD_JSON_LOGS+=("$SHARD_JSON")

  ARGS=(cognify-fanout --shard "${i}/${WORKERS}" --json)
  if [[ -n "$MODEL" ]]; then
    ARGS+=(--model "$MODEL")
  fi
  if [[ -n "$SINCE" ]]; then
    ARGS+=(--since "$SINCE")
  fi
  if [[ -n "$DRY_RUN" ]]; then
    ARGS+=("$DRY_RUN")
  fi

  # The CLI emits per-session progress to stderr and the summary JSON
  # to stdout. We capture both: stderr → live log, stdout → json file.
  ( "$DEVBRAIN_BIN" "${ARGS[@]}" 2>"$SHARD_LOG" >"$SHARD_JSON" ) &
  PIDS+=("$!")
  echo "fanout-backfill: spawned shard $i/${WORKERS} pid=${PIDS[-1]}"
done

# Wait for all shards. Capture exit codes.
declare -a EXITS
FAILED=0
for ((i=0; i<WORKERS; i++)); do
  if wait "${PIDS[$i]}"; then
    EXITS+=("0")
  else
    EXITS+=("$?")
    FAILED=1
    echo "fanout-backfill: shard $i exited non-zero (see ${SHARD_LOGS[$i]})" >&2
  fi
done

# Aggregate the per-shard JSON summaries into one combined file. Each
# shard wrote a top-level object with sessions_* / rows_emitted /
# llm_calls / failure_counts / dry_run. We sum the numeric fields and
# merge the failure_counts maps.
python3 - "$SUMMARY_JSON" "${SHARD_JSON_LOGS[@]}" <<'PYEOF'
import json
import sys

out_path = sys.argv[1]
shard_paths = sys.argv[2:]

agg = {
    "sessions_discovered": 0,
    "sessions_processed":  0,
    "sessions_failed":     0,
    "sessions_skipped":    0,
    "rows_emitted":        0,
    "llm_calls":           0,
    "failure_counts":      {},
    "shards":              len(shard_paths),
    "shard_paths":         shard_paths,
}
dry_run_any = False

for p in shard_paths:
    try:
        with open(p) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        agg.setdefault("read_errors", []).append({"path": p, "error": str(exc)})
        continue
    for k in (
        "sessions_discovered", "sessions_processed", "sessions_failed",
        "sessions_skipped", "rows_emitted", "llm_calls",
    ):
        agg[k] += int(data.get(k, 0) or 0)
    for fk, fv in (data.get("failure_counts") or {}).items():
        agg["failure_counts"][fk] = agg["failure_counts"].get(fk, 0) + int(fv)
    if data.get("dry_run"):
        dry_run_any = True

agg["dry_run"] = dry_run_any

with open(out_path, "w") as f:
    json.dump(agg, f, indent=2)

# Print the summary so the shell wrapper can echo it too.
print(json.dumps(agg, indent=2))
PYEOF

echo ""
echo "fanout-backfill: summary written to $SUMMARY_JSON"

exit $FAILED
