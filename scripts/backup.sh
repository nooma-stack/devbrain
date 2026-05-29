#!/bin/bash
# DevBrain nightly backup.
#
# Three layers, all version-matched via `docker exec` (dump tool == server):
#   1. Logical pg_dump (custom format)  -> point-in-night full restore
#   2. Physical pg_basebackup           -> PITR anchor (replays WAL archive)
#   3. Retention pruning + WAL archive trim (bounds disk)
#
# Restore quickref (see scripts/RESTORE.md):
#   logical: docker exec -i devbrain-db pg_restore -U devbrain -d devbrain --clean < <dump>
#   PITR:    extract a base-*/, set restore_command='cp /wal_archive/%f %p' +
#            recovery_target_time, start server in recovery.
#
# launchd runs this with a minimal env, so paths are absolute and PATH is set.
set -uo pipefail
export PATH="/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"

CONTAINER="devbrain-db"
DB="devbrain"
DBUSER="devbrain"
ROOT="/Users/patrickkelly/devbrain/backups"
DUMPS="$ROOT/dumps"
BASES="$ROOT/base"
LOG="$ROOT/backup.log"

# Retention: WAL window (7d) is kept LONGER than the oldest base (4d) so every
# retained base backup still has the WAL needed to PITR forward from it.
DUMP_RETAIN_DAYS=14
BASE_RETAIN_DAYS=4
WAL_RETAIN_DAYS=7

mkdir -p "$DUMPS" "$BASES"
TS="$(date +%Y%m%d-%H%M%S)"
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG"; }
fail=0

log "=== backup start ($TS) ==="

if ! docker exec "$CONTAINER" pg_isready -U "$DBUSER" >/dev/null 2>&1; then
  log "FATAL: $CONTAINER not ready — aborting"
  exit 1
fi

# ── 1. Logical dump (custom format) + restorability check ────────────────────
DUMP="$DUMPS/devbrain-$TS.dump"
if docker exec "$CONTAINER" pg_dump -U "$DBUSER" -Fc -d "$DB" > "$DUMP" 2>>"$LOG"; then
  if docker exec -i "$CONTAINER" pg_restore --list < "$DUMP" >/dev/null 2>>"$LOG"; then
    log "dump OK: $DUMP ($(du -h "$DUMP" | cut -f1))"
  else
    log "ERROR: dump TOC verify failed: $DUMP"; fail=1
  fi
else
  log "ERROR: pg_dump failed"; rm -f "$DUMP"; fail=1
fi

# ── 2. Physical base backup (PITR anchor) ────────────────────────────────────
docker exec "$CONTAINER" rm -rf /tmp/bb 2>/dev/null
if docker exec "$CONTAINER" pg_basebackup -U "$DBUSER" -D /tmp/bb -Ft -z -Xs -c fast >/dev/null 2>>"$LOG"; then
  if docker cp "$CONTAINER:/tmp/bb" "$BASES/base-$TS" 2>>"$LOG"; then
    log "base OK: $BASES/base-$TS ($(du -sh "$BASES/base-$TS" | cut -f1))"
  else
    log "ERROR: docker cp of base backup failed"; fail=1
  fi
  docker exec "$CONTAINER" rm -rf /tmp/bb 2>/dev/null
else
  log "ERROR: pg_basebackup failed"; fail=1
fi

# ── 3. Retention pruning ─────────────────────────────────────────────────────
find "$DUMPS" -name 'devbrain-*.dump' -type f -mtime "+$DUMP_RETAIN_DAYS" -delete 2>>"$LOG" \
  && log "pruned dumps older than ${DUMP_RETAIN_DAYS}d"
find "$BASES" -maxdepth 1 -name 'base-*' -type d -mtime "+$BASE_RETAIN_DAYS" -exec rm -rf {} + 2>>"$LOG" \
  && log "pruned base backups older than ${BASE_RETAIN_DAYS}d"
# WAL archive lives inside the container's named volume; trim old segments so it
# can't grow unbounded (a failing/over-full archive can stall the DB).
docker exec "$CONTAINER" sh -c "find /wal_archive -type f -mtime +$WAL_RETAIN_DAYS -delete" 2>>"$LOG" \
  && log "pruned WAL archive older than ${WAL_RETAIN_DAYS}d"

if [ "$fail" -eq 0 ]; then
  log "=== backup OK ==="
else
  log "=== backup completed WITH ERRORS (see above) ==="
fi
exit "$fail"
