# Rotating the DevBrain database password

DevBrain's Postgres password lives in three places that must stay in sync:

1. The container (stored in the `devbrain-pgdata` volume after first
   initialization — Postgres will **not** re-read `POSTGRES_PASSWORD`
   from docker-compose.yml on subsequent starts).
2. `config/devbrain.yaml` under `database.password`.
3. `.env` as `DEVBRAIN_DB_PASSWORD` (used by `docker-compose.yml`).

Rotating means updating all three. The safe order preserves your data.

## When to rotate

- `devbrain doctor` flagged `db_password_rotated` as WARN.
- Your install dates from before the installer generated random passwords
  (your yaml still says `password: devbrain-local`).
- You shared a machine and want a fresh credential.

## Recommended: `devbrain rotate-db-password`

```bash
devbrain rotate-db-password
```

What it does (preserves data):

1. Connects with the current password (bails if that fails).
2. Generates a new 256-bit random password.
3. Applies it via `ALTER USER ... PASSWORD ...` inside the container.
4. Verifies the new password works before writing anywhere.
5. Updates `.env` (`DEVBRAIN_DB_PASSWORD`) and `config/devbrain.yaml`
   (`database.password`).
6. Recreates the container so any pending `docker-compose.yml` changes
   (loopback-only port binding, etc.) take effect. Skip with
   `--no-recreate` if you only want to rotate the credential.

Flags:

- `--yes` / `-y` — skip the confirmation prompt.
- `--no-recreate` — rotate but don't touch the container.

## Manual: rotate with shell commands (preserves data)

```bash
cd "$DEVBRAIN_HOME"  # or your devbrain repo root

# 1. Generate a new random password.
NEW_PW="$(openssl rand -hex 32)"

# 2. Apply it inside the running Postgres container. You'll be prompted
#    for the CURRENT password — it's whatever your .env / yaml has today
#    (likely 'devbrain-local' if you're reading this).
docker exec -it devbrain-db \
    psql -U devbrain -d devbrain \
    -c "ALTER USER devbrain PASSWORD '$NEW_PW';"

# 3. Write the new value into .env. Remove any existing line first.
grep -v '^DEVBRAIN_DB_PASSWORD=' .env > .env.tmp && mv .env.tmp .env
printf '\n# Database password — rotated on %s\nDEVBRAIN_DB_PASSWORD=%s\n' \
    "$(date +%Y-%m-%d)" "$NEW_PW" >> .env

# 4. Update config/devbrain.yaml. The installer ships a helper that only
#    touches the database: block (not notification passwords):
awk -v pw="$NEW_PW" '
    /^database:/ { in_db = 1; print; next }
    in_db && /^  password:/ { print "  password: " pw; next }
    /^[^[:space:]#]/ { in_db = 0 }
    { print }
' config/devbrain.yaml > config/devbrain.yaml.tmp \
    && mv config/devbrain.yaml.tmp config/devbrain.yaml

# 5. Recreate the container so (a) anything holding the old password
#    reconnects and (b) the loopback-only port binding added by recent
#    docker-compose.yml updates takes effect. `restart` alone won't
#    pick up port/config changes — you need down + up.
docker compose down
docker compose up -d devbrain-db

# 6. Verify.
./bin/devbrain doctor
```

`NEW_PW` stays in your shell history for this session — close the
terminal after rotating if that bothers you.

## Rotate (nuke and recreate — discards data)

Only do this on a throwaway install where nothing in the DB is worth
keeping. Removes the volume and lets docker-compose re-initialize with
whatever is in `.env`.

```bash
cd "$DEVBRAIN_HOME"
docker compose down
docker volume rm devbrain-pgdata

# Clear the password out of .env and yaml, then re-run install.sh,
# which will detect the absence and generate a fresh one.
sed -i.bak '/^DEVBRAIN_DB_PASSWORD=/d' .env
awk '
    /^database:/ { in_db = 1; print; next }
    in_db && /^  password:/ { print "  password: REPLACE_DURING_INSTALL"; next }
    /^[^[:space:]#]/ { in_db = 0 }
    { print }
' config/devbrain.yaml > config/devbrain.yaml.tmp \
    && mv config/devbrain.yaml.tmp config/devbrain.yaml

./scripts/install.sh
```

## Why Postgres doesn't pick up a changed `POSTGRES_PASSWORD`

`POSTGRES_PASSWORD` is only read the first time the data directory is
empty. Once the volume exists, the password is stored inside it and
environment variable changes are ignored. That's why `ALTER USER` (step
2 above) is required when rotating on a live install.
