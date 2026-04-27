# Migrating DevBrain Memory Between Machines

This is the operator playbook for the canonical use case: you've been
running DevBrain on a MacBook for months, you bought a new Mac Studio,
and you want all of the accumulated memory (projects, decisions,
patterns, issues, raw transcripts, dev/notification config) to come
along.

DevBrain ships two CLI subcommands for this: `export-memory` on the
old machine writes a portable JSON file; `import-memory` on the new
machine reads it back into the destination's database. Both are
idempotent — re-running on the same file is safe.

The pipeline does **not** transfer:

- Postgres binary data — the file is portable JSON, no `pg_dump`.
- Legacy tables (`chunks`, `decisions`, `patterns`, `issues`). Those
  are write-side only post-P2.b; reads come from `devbrain.memory`,
  which the export covers. The destination's own `backfill-memory`
  pass would re-create them anyway.
- Local credentials, `.env`, or `config/devbrain.yaml`. Re-run
  `devbrain setup` on the new machine for those.

---

## 1. Pre-flight — both machines on the same schema

The importer refuses to load an export whose schema version doesn't
match the destination. Before exporting, run on **both** machines:

```bash
bin/devbrain migrate
```

If the source machine has `010_unified_memory.sql` applied but the
destination has only `008_artifact_warning_count.sql`, the import
will reject with a clear "schema mismatch" error — fix it by
upgrading whichever side is older.

## 2. On the old machine — export

```bash
# Everything, gzipped, to ~/devbrain-export.json.gz
bin/devbrain export-memory --out ~/devbrain-export.json.gz

# Or scoped to a single project (repeatable)
bin/devbrain export-memory --out ~/just-acme.json --project acme
```

The output line tells you what landed in the file:

```
[export] wrote /Users/you/devbrain-export.json.gz: projects=4, devs=2, memory=8421, raw_sessions=1107
```

The `.gz` suffix triggers gzip compression automatically; pass
`--gzip` / `--no-gzip` to override.

> The export file contains your raw session transcripts and any
> notification channels you've configured (telegram bot tokens,
> webhook URLs, email addresses). Treat it like a credential dump —
> transfer over a private channel (USB, scp, encrypted cloud
> storage), not a public link.

## 3. Move the file to the new machine

Whatever your preferred private channel is — USB, `scp`, `rsync`,
encrypted cloud share — copy the file to the new machine's home
directory.

```bash
# Example: scp from old → new
scp ~/devbrain-export.json.gz studio.local:~/
```

## 4. On the new machine — install DevBrain first

The destination needs DevBrain installed and the database initialized
before the importer has anywhere to land rows:

```bash
# One-liner installer — sets up Postgres, MCP server, ingest, etc.
curl -fsSL https://raw.githubusercontent.com/nooma-stack/devbrain/main/scripts/install.sh | bash

# Bring the schema up to the same level as the source
cd ~/devbrain && bin/devbrain migrate
```

Run `bin/devbrain devdoctor` to confirm the install is green.

## 5. On the new machine — import

```bash
# Dry-run first — shows what would be inserted without committing
bin/devbrain import-memory --in ~/devbrain-export.json.gz --dry-run

# Real run
bin/devbrain import-memory --in ~/devbrain-export.json.gz
```

Output:

```
[import] projects: 4 resolved
[import] devs: 1 inserted, 1 preserved
[import] raw_sessions: 1107 inserted, 0 dup (of 1107 scanned)
[import] memory: 8421 inserted, 0 dup (of 8421 scanned)
```

`preserved` for devs means: a dev with that `dev_id` already existed
on the destination (e.g., the auto-registered default dev from
`install-identity`), so we left its locally-customized notification
channels alone. The exported channels are only used when the
destination has no row for that dev_id yet. Re-running `import-memory`
on the same file is safe: every per-table insert uses
`ON CONFLICT DO NOTHING`.

## 6. Verify

```bash
# A few smoke checks
bin/devbrain devdoctor
bin/devbrain dashboard           # browse the imported jobs/sessions
```

In an MCP client, `deep_search` for something you remember the old
machine knowing — patterns or decisions should surface.

---

## Troubleshooting

**`schema mismatch: export was produced against … this destination is at …`**
Run `bin/devbrain migrate` on whichever machine is older, then
re-export.

**`unsupported export version N`**
The export came from a newer DevBrain build than the one you
installed. Either upgrade the destination's DevBrain checkout or
re-export from the old machine after `git pull`.

**`unknown project slug(s): foo`**
The `--project foo` flag refers to a slug that isn't in the source
DB. List slugs with `psql -c "SELECT slug FROM devbrain.projects;"`.

**Memory rows show up but `deep_search` doesn't surface them**
The importer doesn't recompute embeddings — it carries the source's
bit-equal vectors over. If the destination's `pgvector` has a
different dimensionality than the source (very rare), the index
will refuse the import. Confirm with
`psql -c "\d+ devbrain.memory"` on both ends.

**Re-import keeps reporting "0 dup" instead of "N dup"**
The natural keys for dedup are `(provenance_id, kind)` for memory and
`(source_app, source_hash)` for raw_sessions. If the source rows
have NULL `provenance_id` (rare ad-hoc curator entries) the partial
unique index can't dedupe them — they'll insert again on every
re-import. That's expected; re-export and clear the destination if
you need true idempotency for those rows.
