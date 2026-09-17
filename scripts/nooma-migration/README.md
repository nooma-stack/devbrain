# Nooma Studio migration

These scripts create a Nooma-only DevBrain dump without modifying the source
database or source backup.

## Build a rehearsal or final artifact

```bash
./scripts/nooma-migration/build-staging.sh \
  --dump backups/dumps/devbrain-YYYYMMDD-HHMMSS.dump \
  --database devbrain_nooma_stage_YYYYMMDD \
  --output backups/nooma-migration/devbrain-nooma-sanitized-YYYYMMDD.dump
```

Use a new staging database name by default. `--replace` is required to drop an
existing disposable staging database or overwrite an existing output file.
The database-name guard refuses to operate on `devbrain` or on any database
outside the `devbrain_nooma_stage_YYYYMMDD[_N]` naming scheme.

The pipeline:

1. verifies the source custom dump's table of contents;
2. restores it into a disposable PostgreSQL 17 database;
3. retains only the explicit Nooma project and developer allowlists;
4. attributes only unambiguous DevBrain/DevBrain Factory orphan paths;
5. removes ambiguous and BrightBot-derived history, including historically
   misattributed rows in otherwise allowed projects;
6. resets transient queues, invitations, notifications, and file locks;
7. prunes and rechains the retained tamper-evident memory ledger;
8. backfills newly attributed chunks and session summaries;
9. validates isolation, row-count floors, references, and ledger integrity;
10. writes a mode-0600 custom dump and verifies its table of contents.

## Final cutover order

1. Finish a rehearsal restore and recall test on the Nooma Studio.
2. Stop source launchd ingest/cognify jobs.
3. set the source `devbrain` database read-only and terminate old sessions;
4. make and verify a final source dump;
5. build a new sanitized artifact from that final dump;
6. restore the artifact into the Nooma Studio's `devbrain` database;
7. validate counts, isolation, recall, launchd services, backups, and reboot;
8. route MCP clients to the Nooma Studio over SSH;
9. leave the MacBook database, volume, and backups intact and read-only as the
   rollback point; do not run both databases as writers.

## Nooma off-LAN endpoint

Nooma mirrors BrightBrain's outbound reverse-tunnel pattern but uses the SOHO
VPS instead of the LHT VPS:

- LAN host: `patrickkelly@192.168.0.4:22` (router-reserved address)
- public onboarding host: `patrickkelly@2.24.99.121:2223`
- tunnel account: `devbrain-tunnel@2.24.99.121` (forced command, no PTY,
  remote-listen restricted to `*:2223`)
- tunnel job: `com.devbrain.tunnel-soho-vps`
- onboarding jobs: `com.devbrain.onboard` and `com.devbrain.reconciler`
- webhook runtime: `.venv-onboard/bin/python` (an isolated uv-managed Python;
  avoids macOS Local Network privacy blocking Homebrew's `Python.app`)

Concrete Nooma launchd plists live in `scripts/nooma-migration/launchd/`.
The generated onboarding kit gets its public SSH user, host, port, and branding
from `config/devbrain.yaml` or the corresponding `DEVBRAIN_ONBOARD_*`
environment variables.
