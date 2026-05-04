# DevBrain Migrations

Sequential SQL migrations applied to `devbrain` schema in `devbrain.devbrain`
Postgres. Source of truth: `devbrain.schema_migrations` ledger table (added
in 009).

## File naming

```
0NN_<short-slug>.sql
```

- Numeric prefix is the sequence; never reuse a number.
- Always idempotent — every `CREATE TABLE`, `CREATE INDEX`, `ALTER TABLE
  ADD COLUMN` uses `IF NOT EXISTS`. This lets the runner re-apply safely
  if the ledger drifts from reality.
- One concern per file. Don't bundle "add a column + seed five rows" in
  the same file unless the seeding is logically part of the schema.

## How migrations are applied

**Two paths, depending on lifecycle stage:**

### First install (fresh DB)

`docker compose up -d devbrain-db` triggers the `pgvector/pgvector:pg17`
entrypoint, which auto-runs every `migrations/*.sql` in lexical order
against the empty DB. No manual step needed. See `INSTALL.md` Step 3.

### After install — applying new migrations

Use the `devbrain migrate` CLI (calls `factory/cli.py migrate` under the
hood). Don't apply via direct `psql`.

```bash
# See what's pending without applying
devbrain migrate --dry-run

# Apply pending migrations
devbrain migrate
```

The runner:
1. Reads `migrations/*.sql` from disk
2. Looks each filename up in `devbrain.schema_migrations`
3. Executes any not-yet-recorded migrations in their own transactions
4. Records each successful apply in the ledger

A Postgres advisory lock prevents two concurrent invocations from
colliding.

## What if I applied directly via `psql` by accident?

`devbrain migrate` self-heals. The schema is already in place (your direct
apply did that). The ledger is the only thing missing. Running `devbrain
migrate` will:

1. Detect the migration as "pending" (not in ledger)
2. Re-run the SQL — idempotent, so it's a no-op
3. INSERT into `schema_migrations` with `ON CONFLICT DO NOTHING`

After that, the ledger matches reality.

If you want to verify ledger health periodically:

```bash
# Lists migrations on disk that aren't yet recorded as applied.
# If empty, the ledger is in sync with disk.
devbrain migrate --dry-run
```

## Cross-instance application

When propagating a migration across instances (laptop → Mac Studio,
laptop → BrightBrain instance, etc.) — pull the new code first
(`git pull`), THEN run `devbrain migrate`. The new SQL file lands on
disk first; the ledger gets updated when the runner sees it pending.

## When to bump the migration number

- Adding a column / index / table → new migration
- Renaming a column → new migration with `ALTER TABLE ... RENAME COLUMN`
- Dropping a deprecated table → new migration with `DROP TABLE IF EXISTS`
- Editing an existing migration file in place: **never**, even pre-merge.
  Once a migration has been applied anywhere (including your local DB
  during dev), edit-in-place desyncs the ledger from reality. If you
  need to fix a mistake, write a new migration that corrects the prior.

## Schema_migrations table itself

Created in `009_schema_migrations.sql`. That migration:
- Creates the `devbrain.schema_migrations` table
- Backfills rows for 001-008 via a trailing `ON CONFLICT INSERT` (so the
  runner doesn't re-execute those files on existing DBs that pre-date
  the ledger)

Do not delete rows from `schema_migrations` manually unless you also
intend to re-apply the corresponding migration file.
