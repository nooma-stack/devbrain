# DevBrain host resilience

DevBrain can install an optional host watchdog for an always-on workstation or
Studio. Local recovery is fully functional without a VPS, cloud account, or
inbound network port.

## Profiles

| Profile | Service | Interval | Default behavior |
|---|---|---:|---|
| `workstation` | User LaunchAgent / user systemd unit | 5 minutes | Opt-in; Docker Desktop recovery on macOS |
| `studio` | macOS system LaunchDaemon running as the non-root install user, or user systemd | 1 minute | Enabled by the main installer; Colima recovery on macOS |

Both profiles check the container runtime, `devbrain-db`, Ollama, disk space,
and any explicitly selected integrations. Ingest is also checked on macOS; it
is disabled on Linux because the currently documented ingest daemon is a
privileged system unit. Recovery has a failure threshold, cooldown, and
maximum-attempt circuit breaker.

Docker checks and Compose recovery are pinned to the selected backend context
(`colima`, `desktop-linux`, `orbstack`, or `default`) so another running Docker
backend cannot satisfy the wrong profile.

## Install choices

Local-only workstation:

```bash
./scripts/install-resilience.sh \
  --profile workstation \
  --container-runtime docker-desktop
```

Local-only Studio (no VPS):

```bash
./scripts/install-resilience.sh \
  --profile studio \
  --container-runtime colima
```

On Linux, use `--container-runtime docker-engine`. Runtime availability is
monitored there, but restarting the privileged Docker daemon is intentionally
left to the host's service policy.

Linux uses a user systemd unit. The `studio` installer enables user lingering
so the unit runs without an interactive login. Uninstall leaves lingering
enabled because other user services may depend on it.

The full DevBrain installer exposes the same choices:

```bash
./scripts/install.sh --profile=studio
./scripts/install.sh --profile=workstation --with-resilience
```

Or use the guided flow:

```bash
devbrain setup resilience
```

### Optional outbound heartbeat

Set these in the repository's mode-0600 `.env`:

```dotenv
DEVBRAIN_HEARTBEAT_URL=https://monitor.example.com/devbrain/heartbeat
DEVBRAIN_HEARTBEAT_TOKEN=replace-with-random-secret
```

Then install with `--with-heartbeat`. Heartbeats are outbound HTTPS POSTs and
the token is required: DevBrain signs the canonical JSON body with HMAC-SHA256.
The token must live in the repository's `.env` so the background service can
load it after reboot. Secret values are not copied into the service definition
or install manifest.

The receiving monitor is responsible for its own dead-man timer. A missing or
late request should alert independently of the monitored host.

### Optional tunnel and agent-bus checks

These checks do not install a VPS, tunnel, or agent-bus daemon. They monitor
services the operator has already installed:

```bash
DEVBRAIN_TUNNEL_LABEL=com.example.devbrain-tunnel \
./scripts/install-resilience.sh \
  --profile studio \
  --container-runtime colima \
  --with-tunnel-check

./scripts/install-resilience.sh \
  --profile studio \
  --container-runtime colima \
  --with-agent-bus-check \
  --agent-bus-url http://127.0.0.1:18900/healthz
```

The agent-bus health probe is TCP-only. DevBrain does not copy or expose an
agent-bus credential.

### Optional backup-freshness check

DevBrain does not currently install or schedule a generic backup producer.
Enable freshness monitoring only when another job is already writing backups,
and name that output path explicitly:

```bash
./scripts/install-resilience.sh \
  --profile studio \
  --container-runtime colima \
  --with-backup-check \
  --backup-path /absolute/path/to/backups
```

`DEVBRAIN_BACKUP_PATH` can supply the path instead. A normal Studio install
does not add this check, so it cannot report a permanent failure for a backup
job that was never configured.

## Recovery matrix

| Check | Automatic recovery |
|---|---|
| Docker-compatible runtime | Start configured Colima, Docker Desktop, or OrbStack; Linux Docker Engine is detection-only |
| `devbrain-db` | `docker compose up -d devbrain-db` in the configured repository |
| Ollama | Start the Homebrew service on macOS; detection-only on Linux |
| Ingest | Kick-start the exact launchd label on macOS; disabled by default on Linux because the documented daemon is a privileged system unit |
| Configured tunnel | Kick-start/restart its exact declared service |
| Disk space | Detection only; DevBrain never auto-deletes data |
| Backup freshness | Detection only; a stale backup never triggers deletion or overwrite |
| Agent bus | Detection only |

The runtime has no generic command or shell recovery type. Configuration keys
such as `command`, `cmd`, `argv`, and `shell` are rejected.

## Signed medic (advanced)

The medic is an optional filesystem queue. It does not expose a listener and
does not require a VPS. Health and queue schedules are independent, and it
checks at most one queued task every 10 seconds so a backlog is never processed
as an unbounded batch. Status and diagnosis tasks require one primary signature.
A heal task requires:

1. Two distinct authorized Ed25519 keys (`primary` and `confirm`).
2. Signatures over the same complete canonical task body.
3. A matching instance ID.
4. An unexpired task and unused nonce.
5. A check explicitly listed in the host's medic allowlist.
6. A typed recovery already declared in the local watchdog configuration.
7. A fresh failed health check plus the same failure-threshold, cooldown, and
   maximum-attempt policy used by automatic recovery.

There is no shell, reboot, arbitrary configuration write, screenshot, or
AI-generated command action.

Generate the keys on an operator machine—not on the Studio:

```bash
devbrain ops medic keygen \
  --private ~/.config/devbrain-medic/primary.key \
  --public ~/.config/devbrain-medic/primary.pub

devbrain ops medic keygen \
  --private ~/.config/devbrain-medic/confirm.key \
  --public ~/.config/devbrain-medic/confirm.pub
```

Back up both private files securely. Copy only the `.pub` files to the host,
then choose diagnosis-only mode:

```bash
./scripts/install-resilience.sh \
  --profile studio \
  --container-runtime colima \
  --with-medic diagnose \
  --medic-instance-id nooma-studio \
  --medic-primary-public-key /path/to/primary.pub
```

Or explicitly enable selected heal actions:

```bash
./scripts/install-resilience.sh \
  --profile studio \
  --container-runtime colima \
  --with-medic heal \
  --medic-instance-id nooma-studio \
  --medic-primary-public-key /path/to/primary.pub \
  --medic-confirm-public-key /path/to/confirm.pub \
  --medic-allow-check postgres \
  --medic-allow-check ollama
```

Create a short-lived task on the operator machine:

```bash
devbrain ops medic task \
  --instance-id nooma-studio \
  --action heal \
  --check postgres \
  --sign operator-primary=~/.config/devbrain-medic/primary.key \
  --sign operator-confirm=~/.config/devbrain-medic/confirm.key \
  --output heal-postgres.json
```

Deliver the JSON file through an operator-controlled channel into:

```text
~/.devbrain/resilience/medic-queue/inbox/
```

Deliver it under a non-`.json` temporary name, then rename it to `.json` on the
same filesystem. That atomic handoff prevents the watcher from claiming a
partially copied file.

That channel can be local copy, LAN SSH, an existing secure file-sync system,
or a separately managed tunnel. DevBrain deliberately does not create an
internet-facing medic endpoint.

Medic result and archive files are local audit records, not host-signed
attestations; authenticate any transport used to retrieve them. Disabling,
re-keying, or uninstalling the medic atomically moves pending inbox/processing
content under `medic-queue/quarantine/`. Outbox, archive, rejected, replay, and
quarantine history are retained intentionally for audit and are never executed.

## Status, fire drill, and uninstall

```bash
devbrain ops status
devbrain ops run-once
devbrain devdoctor
```

Recommended fire drill:

1. Record a healthy `devbrain ops status`.
2. Stop one non-destructive test target, such as Ollama.
3. Run two foreground cycles (the default failure threshold is two).
4. Confirm the typed recovery and a new healthy heartbeat.
5. Confirm any external dead-man monitor receives heartbeats.

The installer writes an exact artifact manifest at:

```text
~/.devbrain/resilience/install-manifest.json
```

The main installer also records the selected container runtime and exact
Docker context at `~/.devbrain/install-target.json`, even when resilience is
disabled. Idempotent installer reruns preserve that target and refuse to
switch runtimes without a separate database migration.

`scripts/reinstall.sh` uses the recorded target so it cannot silently clean
the ambient Docker context while leaving the real DevBrain database behind.
Explicit target flags must agree with existing metadata. For an older install
without metadata, pass both
`--container-runtime` and `--docker-context`; reinstall fails closed if it
cannot resolve the target.

`devbrain upgrade` refreshes the root Python requirements and, when this
manifest exists, restarts the installed resilience service without replacing
its runtime configuration. Password-rotation container recreation is pinned
to the recorded Docker context (or to the single context where `devbrain-db`
can be proven to exist for a legacy install). The command wrapper updates
before loading the upgrade Python code. For an installation that predates
that wrapper behavior, bootstrap it once with:

```bash
cd ~/devbrain
git pull --ff-only
./bin/devbrain upgrade --no-pull
```

Uninstall removes only paths named by that manifest:

```bash
./scripts/install-resilience.sh --uninstall --yes
```
