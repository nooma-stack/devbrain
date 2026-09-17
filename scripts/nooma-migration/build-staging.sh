#!/bin/bash
# Restore a verified source dump into a disposable database, sanitize it for
# Nooma, backfill newly attributed DevBrain history, validate it, and emit a
# transfer-ready custom-format dump.
set -euo pipefail

export PATH="/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"

ROOT="/Users/patrickkelly/devbrain"
CONTAINER="devbrain-db"
DBUSER="devbrain"
STAGE_DB="devbrain_nooma_stage_$(date +%Y%m%d)"
DUMP=""
OUTPUT=""
REPLACE=0

usage() {
    echo "Usage: $0 --dump PATH [--database devbrain_nooma_stage_YYYYMMDD[_N]] [--output PATH] [--replace]"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dump)
            DUMP="${2:-}"
            shift 2
            ;;
        --database)
            STAGE_DB="${2:-}"
            shift 2
            ;;
        --output)
            OUTPUT="${2:-}"
            shift 2
            ;;
        --replace)
            REPLACE=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ -z "$DUMP" ] || [ ! -f "$DUMP" ]; then
    echo "--dump must name an existing custom-format dump" >&2
    exit 2
fi

if [[ ! "$STAGE_DB" =~ ^devbrain_nooma_stage_[0-9]{8}(_[0-9]+)?$ ]]; then
    echo "Unsafe staging database name: $STAGE_DB" >&2
    exit 2
fi

if [ -z "$OUTPUT" ]; then
    OUTPUT="$ROOT/backups/nooma-migration/devbrain-nooma-sanitized-${STAGE_DB#devbrain_nooma_stage_}.dump"
fi

if [ -e "$OUTPUT" ] && [ "$REPLACE" -ne 1 ]; then
    echo "Output already exists; pass --replace to overwrite it: $OUTPUT" >&2
    exit 2
fi

if ! docker exec "$CONTAINER" pg_isready -U "$DBUSER" >/dev/null 2>&1; then
    echo "$CONTAINER is not ready" >&2
    exit 1
fi

echo "Verifying source dump TOC..."
docker exec -i "$CONTAINER" pg_restore --list < "$DUMP" >/dev/null

db_exists="$(docker exec "$CONTAINER" psql -U "$DBUSER" -d postgres -Atc \
    "SELECT 1 FROM pg_database WHERE datname = '$STAGE_DB'")"

if [ "$db_exists" = "1" ]; then
    if [ "$REPLACE" -ne 1 ]; then
        echo "Staging database already exists; pass --replace to recreate it: $STAGE_DB" >&2
        exit 2
    fi
    echo "Dropping the explicitly named disposable database $STAGE_DB..."
    docker exec "$CONTAINER" dropdb -U "$DBUSER" --force "$STAGE_DB"
fi

echo "Creating $STAGE_DB..."
docker exec "$CONTAINER" createdb -U "$DBUSER" -T template0 "$STAGE_DB"

echo "Restoring $(basename "$DUMP") into $STAGE_DB..."
docker exec -i "$CONTAINER" pg_restore \
    -U "$DBUSER" \
    -d "$STAGE_DB" \
    --no-owner \
    --no-privileges \
    < "$DUMP"

echo "Applying Nooma isolation sanitizer..."
docker exec -i "$CONTAINER" psql \
    -U "$DBUSER" \
    -d "$STAGE_DB" \
    -v ON_ERROR_STOP=1 \
    < "$ROOT/scripts/nooma-migration/sanitize-staging.sql"

if [ ! -f "$ROOT/.env" ]; then
    echo "Missing $ROOT/.env; cannot connect the backfill command to staging" >&2
    exit 1
fi

set -a
# shellcheck disable=SC1091
source "$ROOT/.env"
set +a

: "${DEVBRAIN_DB_PASSWORD:?DEVBRAIN_DB_PASSWORD is required in .env}"
stage_port="${DEVBRAIN_DB_HOST_PORT:-5433}"
stage_url="postgresql://${DBUSER}@127.0.0.1:${stage_port}/${STAGE_DB}"

echo "Backfilling newly attributed chunk memory into staging..."
PGPASSWORD="$DEVBRAIN_DB_PASSWORD" DEVBRAIN_DATABASE_URL="$stage_url" \
    "$ROOT/bin/devbrain" backfill-memory --only chunks

echo "Backfilling newly attributed raw-session summaries into staging..."
PGPASSWORD="$DEVBRAIN_DB_PASSWORD" DEVBRAIN_DATABASE_URL="$stage_url" \
    "$ROOT/bin/devbrain" backfill-memory --only raw_sessions

echo "Validating isolation, row floors, runtime reset, and ledger integrity..."
docker exec -i "$CONTAINER" psql \
    -U "$DBUSER" \
    -d "$STAGE_DB" \
    -v ON_ERROR_STOP=1 \
    < "$ROOT/scripts/nooma-migration/validate-staging.sql"

mkdir -p "$(dirname "$OUTPUT")"
umask 077
tmp_output="$(mktemp "$(dirname "$OUTPUT")/.devbrain-nooma-dump.XXXXXX")"
cleanup_partial() {
    rm -f "$tmp_output"
}
trap cleanup_partial EXIT

echo "Writing transfer dump to $OUTPUT..."
docker exec "$CONTAINER" pg_dump -U "$DBUSER" -Fc -d "$STAGE_DB" > "$tmp_output"
docker exec -i "$CONTAINER" pg_restore --list < "$tmp_output" >/dev/null
mv "$tmp_output" "$OUTPUT"
trap - EXIT

echo "Rehearsal artifact ready:"
ls -lh "$OUTPUT"
echo "Disposable staging database retained for inspection: $STAGE_DB"
