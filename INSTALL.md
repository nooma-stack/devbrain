# Installing DevBrain

> **Most users want the one-liner**, not these manual steps. From a
> fresh macOS or Linux machine:
>
> ```bash
> curl -fsSL https://raw.githubusercontent.com/nooma-stack/devbrain/main/scripts/install.sh | bash
> ```
>
> Then run `cd ~/devbrain && ./bin/devbrain setup` for the interactive
> wizard. The sections below are the manual reference for when you
> want to understand or customize each step.

---


DevBrain is a local-first persistent memory and dev-automation system. It runs
entirely on your machine: a Postgres database (in Docker), an Ollama server
(native), a Node-based MCP server, and a Python ingest watcher.

This guide walks you from a fresh clone to a green `devbrain doctor`.

---

## 1. Prerequisites

Install these **before** cloning the repo. DevBrain's installer does not
install them for you.

| Component | Minimum version | Why |
|-----------|-----------------|-----|
| macOS 13+ or Linux (x86_64 / arm64) | — | Supported platforms |
| Docker Engine | 24+ | Runs the Postgres + pgvector container |
| Python | 3.11+ | Ingest watcher, CLI, `devbrain doctor` |
| Node.js | 20+ | MCP server (TypeScript, compiled to `dist/`) |
| npm | 10+ | Ships with Node 20 |
| Ollama | 0.3+ | Native embedding + summarization server |
| Git | any recent | Cloning and hooks |
| `psql` client | any recent | Smoke tests (optional but useful) |

### Disk & memory

- Roughly **10 GB** of free disk: ~7 GB for Ollama models, ~1 GB for the
  Postgres volume, ~2 GB for `node_modules` and Python venvs.
- 16 GB RAM recommended while `qwen2.5:7b` is loaded.

### Install the prerequisites

**macOS (Homebrew):**

```bash
brew install --cask docker
brew install python@3.11 node@20 ollama git libpq
brew link --force libpq   # puts psql on PATH
```

Launch Docker Desktop once so the daemon starts.

If you prefer a non-Desktop runtime (licensing, resource overhead), either
of these works and still exposes a `docker` CLI:

```bash
brew install colima && colima start          # OR
brew install --cask orbstack && open -a OrbStack
```

**Linux (Debian/Ubuntu example):**

```bash
# Docker Engine
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # log out/in after this

# Python 3.11, Node 20, build basics
sudo apt-get update
sudo apt-get install -y python3.11 python3.11-venv python3-pip \
    git postgresql-client build-essential curl
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt-get install -y nodejs

# Ollama
curl -fsSL https://ollama.com/install.sh | sh
```

Verify:

```bash
docker --version
python3.11 --version
node --version          # must be v20.x or newer
npm --version
ollama --version
git --version
psql --version
```

### AI CLI authentication

The factory shells out to whichever AI CLIs are on `$PATH`. Install one
or more, then complete each tool's auth flow before running the wizard
so the factory can spawn them without prompting:

```bash
claude login              # Anthropic Claude Code (OAuth)
codex login               # OpenAI Codex (OAuth)
gemini auth login         # Google Gemini CLI (OAuth)
```

API-key alternatives are also supported — drop `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, or `GEMINI_API_KEY` into `.env` and the wizard will
pick them up. At least one CLI is required for `factory_plan`; the
wizard prompts for the rest. If you skip this step, the factory falls
back to whichever credential set is wired up at first invocation.

---

## 2. Quick start (TL;DR)

For the impatient. Assumes every prerequisite above is already installed.

```bash
git clone <repo-url> devbrain && cd devbrain
cp .env.example .env                                  # edit if needed
cp config/devbrain.yaml.example config/devbrain.yaml  # edit project_mappings
docker compose up -d devbrain-db
ollama pull snowflake-arctic-embed2 && ollama pull qwen2.5:7b
(cd mcp-server && npm install && npm run build)
(cd ingest && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./bin/devbrain doctor
./bin/devbrain register --dev-id "$USER"
```

If anything fails, follow the detailed steps below — they explain the *why*
and include troubleshooting.

---

## 3. Detailed step-by-step

### Step 1 — Clone the repo

```bash
git clone <repo-url> devbrain
cd devbrain
```

All commands below assume you are in the repo root (`$DEVBRAIN_HOME`).

### Step 2 — Configure environment variables

Copy the template and open it in an editor:

```bash
cp .env.example .env
${EDITOR:-nano} .env
```

Every variable in `.env` is **optional**. Precedence is:

> `.env` / shell env  →  `config/devbrain.yaml`  →  built-in defaults

The defaults work on a single-user localhost install. Override only what
you need:

| Variable | Purpose | Default |
|----------|---------|---------|
| `DEVBRAIN_HOME` | Absolute path to the repo. Used by the MCP launcher, launchd plist template, and Python loaders. | Inferred from each entrypoint's location |
| `DEVBRAIN_CONFIG` | Path to `devbrain.yaml`. | `$DEVBRAIN_HOME/config/devbrain.yaml` |
| `DEVBRAIN_DATABASE_URL` | Full Postgres URL. Overrides `database.*` in YAML. | `postgresql://devbrain:devbrain-local@localhost:5433/devbrain` |
| `DEVBRAIN_DB_HOST_PORT` | Host port the DB container publishes (compose-only). Use this if 5433 conflicts. | `5433` |
| `DEVBRAIN_DB_USER` / `DEVBRAIN_DB_PASSWORD` / `DEVBRAIN_DB_NAME` | Compose-only DB bootstrap credentials. Must match what's in your connection URL. | `devbrain` / `devbrain-local` / `devbrain` |
| `DEVBRAIN_OLLAMA_URL` | Ollama server URL. | `http://localhost:11434` |
| `DEVBRAIN_EMBEDDING_MODEL` | Model pulled in Ollama for embeddings. | `snowflake-arctic-embed2` |
| `DEVBRAIN_SUMMARY_MODEL` | Model for summarization and NL queries. | `qwen2.5:7b` |
| `DEVBRAIN_PROJECT` | Project slug used by session-start hooks. | unset |
| `TELEGRAM_BOT_TOKEN` | Overrides YAML when using Telegram notifications. | unset |

Example `.env` for a host that already has Postgres on 5432:

```dotenv
DEVBRAIN_DB_HOST_PORT=5433
DEVBRAIN_DATABASE_URL=postgresql://devbrain:devbrain-local@localhost:5433/devbrain
DEVBRAIN_OLLAMA_URL=http://localhost:11434
```

### Step 3 — Start PostgreSQL (with pgvector)

DevBrain ships a pinned `pgvector/pgvector:pg17` compose service. On first
start it runs every SQL file in `migrations/` automatically, including
`CREATE EXTENSION vector`.

```bash
docker compose up -d devbrain-db
```

Wait for the healthcheck to pass, then verify:

```bash
docker compose ps devbrain-db
# STATUS should read "Up ... (healthy)"

docker exec -it devbrain-db psql -U devbrain -d devbrain \
    -c "SELECT extname FROM pg_extension WHERE extname='vector';"
# Expected: extname --- vector
```

If you want to connect with a local `psql`:

```bash
psql "postgresql://devbrain:devbrain-local@localhost:5433/devbrain" -c '\dt'
```

**Troubleshooting**

- *"Bind for 0.0.0.0:5433 failed: port is already allocated"* — another
  Postgres is on 5433. Set `DEVBRAIN_DB_HOST_PORT=5434` in `.env` (and
  update `DEVBRAIN_DATABASE_URL` to match), then `docker compose up -d`.
- *`pgvector` extension missing* — you probably overrode the image. The
  image **must** be `pgvector/pgvector:pg17`; plain `postgres:17` does not
  include the extension.
- *Migrations did not run* — migrations only run when the volume is empty.
  To reset: `docker compose down -v && docker compose up -d devbrain-db`
  (this wipes all DevBrain data).

### Step 4 — Configure `devbrain.yaml`

```bash
cp config/devbrain.yaml.example config/devbrain.yaml
${EDITOR:-nano} config/devbrain.yaml
```

The real `config/devbrain.yaml` is gitignored so you can keep secrets
local. At minimum, fill in `ingest.project_mappings` so adapters can
attribute sessions to the right project:

```yaml
ingest:
  project_mappings:
    "/home/alice/code/myproject": myproject
    "/home/alice/code/another":   another
```

Use **absolute paths**; `~` is expanded at load time. Longest-prefix match
wins. If you plan to use the dev factory, also fill `factory.project_paths`
so the cleanup agent can find each project's checkout.

YAML is indentation-sensitive. If `devbrain doctor` later reports
`config_file FAIL parse error`, you almost certainly have a tab or a
misaligned key.

### Step 5 — Install Ollama and pull models

Ollama runs natively (not in Docker) so it can use Apple Metal / CUDA.

**macOS:** `brew install ollama && brew services start ollama`

**Linux:** the `curl | sh` installer from Step 1 registers a systemd
service. Start it with `sudo systemctl enable --now ollama`.

Verify the server is up:

```bash
curl -s http://localhost:11434/api/tags | head -c 200
# Expect JSON (possibly empty "models": []).
```

Pull the two required models. **This is ~7 GB of download combined.**

```bash
ollama pull snowflake-arctic-embed2   # ~1.2 GB, embeddings
ollama pull qwen2.5:7b                # ~4.7 GB, summaries + NL query
```

Confirm:

```bash
ollama list
# Expect both models listed.
```

If you want to use different models, set `DEVBRAIN_EMBEDDING_MODEL` /
`DEVBRAIN_SUMMARY_MODEL` **and** pull those models. Note: changing the
embedding model requires rebuilding the vector index (see
`ingest/reembed.py`).

### Step 6 — Build the MCP server

```bash
cd mcp-server
npm install
npm run build
cd ..
```

This produces `mcp-server/dist/index.js`, which `run.sh` launches. Rebuild
any time you update TypeScript sources.

**Troubleshooting**

- *`Unsupported engine` or TS errors* — your Node is older than 20. Upgrade
  (`brew upgrade node` or `n lts`) and retry.
- *Peer dependency warnings* — safe to ignore as long as `dist/index.js`
  was produced.

### Step 7 — Create the ingest virtualenv

The ingest watcher is a plain Python script with three deps.

```bash
cd ingest
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
cd ..
```

Sanity-check it imports:

```bash
./ingest/.venv/bin/python -c "import psycopg2, watchdog, yaml; print('ok')"
```

The CLI (`./bin/devbrain`) uses a **separate** venv at the repo root. Set
it up the same way if you have not already:

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

### Step 8 — (Optional, macOS) Install the launchd ingest service

Runs `ingest/main.py watch` in the background, auto-restarting on crash
and on reboot.

```bash
./scripts/install-ingest-service.sh
```

The installer substitutes `@DEVBRAIN_HOME@` in
`com.devbrain.ingest.plist.template` and writes
`~/Library/LaunchAgents/com.devbrain.ingest.plist`, then `launchctl load`s
it. Logs go to `logs/ingest.log` and `logs/ingest.err.log`.

Verify:

```bash
launchctl list | grep com.devbrain.ingest
tail -f logs/ingest.log
```

**Linux alternative.** No launchd. You have two options:

1. Run in the foreground (fine for a dev box):
   ```bash
   ./ingest/.venv/bin/python -u ingest/main.py watch
   ```
2. Write a systemd unit at `/etc/systemd/system/devbrain-ingest.service`:

   ```ini
   [Unit]
   Description=DevBrain ingest watcher
   After=network.target docker.service

   [Service]
   Type=simple
   WorkingDirectory=/absolute/path/to/devbrain/ingest
   ExecStart=/absolute/path/to/devbrain/ingest/.venv/bin/python -u /absolute/path/to/devbrain/ingest/main.py watch
   Restart=always
   Environment=DEVBRAIN_HOME=/absolute/path/to/devbrain

   [Install]
   WantedBy=default.target
   ```
   Then `sudo systemctl daemon-reload && sudo systemctl enable --now devbrain-ingest`.

### Step 9 — Run `devbrain doctor`

```bash
./bin/devbrain doctor
```

This must exit `0`. It is the canonical "is my install healthy?" check.
See Section 4 below for what it validates and how to interpret failures.

### Step 10 — Register yourself as a dev

Required once so the notification system knows who you are.

```bash
./bin/devbrain register --dev-id "$USER"
```

Add notification channels (repeatable `--channel TYPE:ADDRESS`):

```bash
./bin/devbrain register \
  --dev-id alice \
  --name "Alice Example" \
  --channel tmux:local \
  --channel smtp:alice@example.com
```

Supported types: `tmux`, `smtp`, `gmail_dwd`, `gchat_dwd`, `telegram_bot`,
`webhook_slack`, `webhook_discord`, `webhook_generic`. You can add more
later with `./bin/devbrain add-channel --channel TYPE:ADDRESS`.

---

## 4. Verifying the install

`devbrain doctor` runs these checks, in order:

| Check | What it means |
|-------|---------------|
| `devbrain_home` | `$DEVBRAIN_HOME` resolves to a real directory. |
| `config_file` | `config/devbrain.yaml` exists and parses as YAML. |
| `postgres_reachable` | A TCP connection to `DEVBRAIN_DATABASE_URL` succeeds within 3s. |
| `pgvector_installed` | Extension `vector` is present in the target DB. |
| `ollama_reachable` | `GET $DEVBRAIN_OLLAMA_URL/api/tags` returns in 3s. |
| `ollama_model:<name>` | Each of the embedding and summary models is pulled. |
| `mcp_server_built` | `mcp-server/dist/index.js` exists. |
| `ingest_venv` | `ingest/.venv/bin/python` exists. |
| `env_overrides` | Informational — lists every `DEVBRAIN_*` var in your env. |

### Clean output

```
DevBrain doctor
============================================================
  ✅ devbrain_home                   /home/alice/devbrain
  ✅ config_file                     /home/alice/devbrain/config/devbrain.yaml
  ✅ postgres_reachable              localhost:5433/devbrain
  ✅ pgvector_installed              extension 'vector' present
  ✅ ollama_reachable                http://localhost:11434
  ✅ ollama_model:snowflake-arctic-embed2   have snowflake-arctic-embed2:latest
  ✅ ollama_model:qwen2.5:7b         have qwen2.5:7b
  ✅ mcp_server_built                /home/alice/devbrain/mcp-server/dist/index.js
  ✅ ingest_venv                     /home/alice/devbrain/ingest/.venv/bin/python
  ✅ env_overrides                   (none — using yaml + defaults)

✅ All checks passed.
```

### Failure output

```
  ❌ postgres_reachable              could not connect to server: Connection refused
  ❌ pgvector_installed              skipped — DB unreachable
  ...
❌ 2 check(s) failed. See INSTALL.md for setup steps.
```

Exit code is `1` when anything fails, `0` otherwise.

### Machine-readable output

For scripts and CI, use `--json`:

```bash
./bin/devbrain doctor --json
```

Produces an array of `{name, status, detail}` objects. `status` is one of
`PASS`, `WARN`, `FAIL`. Exit code still reflects overall health, so:

```bash
./bin/devbrain doctor --json > health.json || echo "install broken"
```

---

## 5. Connecting an MCP client (Claude Code etc.)

DevBrain's MCP server speaks stdio. Point any MCP-compatible client at
`mcp-server/run.sh`. The script resolves `DEVBRAIN_HOME` from its own
location, so you can move the repo without updating the client config.

Example MCP client config:

```json
{
  "mcpServers": {
    "devbrain": {
      "command": "/absolute/path/to/devbrain/mcp-server/run.sh",
      "args": []
    }
  }
}
```

For Claude Code, drop that under `mcpServers` in `~/.claude/settings.json`
(or your project's `.mcp.json`). Restart the client; you should see tools
like `get_project_context`, `deep_search`, and `store` become available.

Smoke-test the server by hand:

```bash
./mcp-server/run.sh < /dev/null
# Exits immediately on EOF; confirms the binary is runnable.
```

### 5.1 Optional: Agent Bus

DevBrain integrates with **PKRelay**, a companion browser bus that lets
agents see and interact with web pages over MCP. Skip if you only need
local memory and the factory.

```bash
git clone https://github.com/nooma-stack/pkrelay.git ~/pkrelay
(cd ~/pkrelay/mcp-server && npm install && npm run build && npm link)
(cd ~/pkrelay/native-host && bash install.sh)
```

This installs a `pkrelay` binary on PATH, registers a Chrome
native-messaging host, and exposes browser tools (page snapshots,
clicks, form fills) to any MCP client. The DevBrain installer offers
the same flow under "PKRelay (optional)" — opt out with
`--no-pkrelay` if scripting an unattended install.

### 5.2 Multi-dev setup

Two different multi-dev models are supported:

**Model 1 — Devs SSH into a shared host (recommended for teams).** Devs
share a Mac Studio (or similar) running DevBrain locally. Each dev
brings their own AI CLI subscription via per-dev HOME profiles managed
by `devbrain login` / `logins` / `logout`. See
[docs/ONBOARDING_TEAMMATE.md](docs/ONBOARDING_TEAMMATE.md) for the
end-to-end onboarding playbook.

**Model 2 — Each dev runs DevBrain locally, points at a shared Postgres.**
Use the multi-dev wizard below. Tests the connection first, then writes
`DEVBRAIN_DATABASE_URL` to `.env`. Because env wins over yaml in
`build_database_url`, the new URL takes effect immediately on the next
DevBrain process start — no yaml edit required.

**Interactive (recommended):**

```bash
devbrain setup multi-dev
```

Prompts for host, port, database, username, password. Connection failure
leaves `.env` untouched.

**Scripted (CI / Ansible / unattended):**

```bash
devbrain setup-multi-dev \
    --host db.team.example.com --port 5432 \
    --database devbrain --username alice --password "$DB_PASSWORD"
```

> **Security note:** the `--password` flag is visible in `ps aux` for the
> brief window the command runs. For unattended installs, prefer a
> wrapper that reads the secret from a vault / sops file and exports it
> as `$DB_PASSWORD` only for the duration of the call, rather than
> hard-coding the value in a script.

The command exits non-zero if the connection test fails (`.env` is left
alone), so it's safe to chain in pipelines: `devbrain setup-multi-dev ...
&& devbrain devdoctor`.

After running either form, restart any long-lived DevBrain processes
(launchd ingest service, MCP server) so they pick up the new URL.

### 5.3 Migrating between machines

Replacing your machine? `bin/devbrain export-memory --out file.json.gz`
on the old box and `bin/devbrain import-memory --in file.json.gz` on
the new one will carry projects, memory, raw transcripts, and
notification config across — no `pg_dump` required, idempotent on
re-run, locally-customized channels preserved on the destination.

See [docs/MIGRATING.md](docs/MIGRATING.md) for the full operator
playbook including pre-flight schema checks, scoped exports
(`--project SLUG`), and troubleshooting.

---

## 6. Platform notes

### macOS (primary)

All features work. Tested on Apple Silicon. Metal-accelerated Ollama.
`install-ingest-service.sh` sets up launchd.

### Linux (supported, minor caveats)

- No launchd. Either run `ingest/main.py watch` in the foreground or
  install the systemd unit shown in Step 8.
- Ollama works via the official installer; GPU acceleration requires the
  NVIDIA container toolkit / proper driver stack if you want CUDA.
- `install-ingest-service.sh` intentionally exits on non-Darwin.
- Everything else (Docker, Python, Node, MCP server, CLI) is identical.

### Windows

**Not supported in v0.1.** Development may work under WSL2 (the Linux
instructions should transfer), but it is untested and unsupported. File
watching across the WSL/Windows filesystem boundary is known to be
unreliable.

---

## 7. Troubleshooting

Each row maps a `devbrain doctor` failure to its fix.

| Failing check | Likely cause | Fix |
|---------------|--------------|-----|
| `postgres_reachable` | DB container not running | `docker compose up -d devbrain-db` |
| `postgres_reachable` | Port conflict on 5433 | Set `DEVBRAIN_DB_HOST_PORT=5434` and matching `DEVBRAIN_DATABASE_URL` in `.env`, then `docker compose up -d` |
| `postgres_reachable` | Docker daemon not running | Start Docker Desktop / `sudo systemctl start docker` |
| `postgres_reachable` | Wrong password in `.env` | `DEVBRAIN_DATABASE_URL` creds must match `DEVBRAIN_DB_USER` / `DEVBRAIN_DB_PASSWORD` |
| `pgvector_installed` | Wrong image | Must be `pgvector/pgvector:pg17`. Edit `docker-compose.yml` back to the pinned image, `docker compose down -v && docker compose up -d devbrain-db` (destroys data) |
| `pgvector_installed` | Volume predates migrations | `docker compose down -v && docker compose up -d devbrain-db` |
| `ollama_reachable` | Service not running | macOS: `brew services start ollama`. Linux: `sudo systemctl start ollama` |
| `ollama_reachable` | Custom port/URL mismatch | Set `DEVBRAIN_OLLAMA_URL=http://host:port` in `.env` |
| `ollama_model:<name>` | Model not pulled | `ollama pull <name>` — the failure row tells you the exact command |
| `mcp_server_built` | Forgot `npm run build` | `cd mcp-server && npm install && npm run build` |
| `mcp_server_built` | Node too old | Install Node 20+, delete `mcp-server/node_modules`, rebuild |
| `ingest_venv` | Skipped Step 7 | `cd ingest && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` |
| `config_file` | YAML parse error | Indentation — replace tabs with spaces, align keys |
| `config_file` | File missing | `cp config/devbrain.yaml.example config/devbrain.yaml` |

### Postgres password drift

If `devbrain doctor` reports `postgres_reachable` failing with
`password authentication failed` after the container ran fine
previously, the Postgres role's password has drifted from the value in
`.env` / `config/devbrain.yaml`. This typically happens after restoring
from a different `.env`, switching branches that ship a different
default credential, or running `docker compose down -v` partway.

The dev-doctor variant resolves this interactively — it prompts for
the live container password (or rotates it) and rewrites both
`.env` and the YAML in one step:

```bash
./bin/devbrain devdoctor --fix
```

Re-run `./bin/devbrain doctor` afterwards; `postgres_reachable` should
flip to PASS without any manual SQL.

### Credential rotation

Run `devbrain rotate-db-password` to rotate the DevBrain Postgres password.
The command auto-reloads registered cred-dependent processes (ingest
daemon, etc.) and verifies they re-authenticated. If any reload fails,
the rotation rolls back atomically — old creds remain authoritative.

Manual-restart dependents (Claude Desktop MCP servers, running shells
with `DEVBRAIN_DB_PASSWORD` exported) cannot be programmatically reloaded.
The rotation will print which ones need manual action.

To register a custom cred-dependent process, add an entry to
`factory.cred_dependents` in `config/devbrain.yaml`. See the example
template for the schema.

Flags:
- `--skip-dependents` — bypass the registry (legacy single-step behavior).
- `--no-require-all-healthy` — rotate even if some dependents are
  already broken (won't make things worse).

### Common non-doctor issues

- **`./bin/devbrain: command not found`** — you are not in the repo root.
  Either `cd` there or call it by absolute path.
- **`ModuleNotFoundError: click`** — the root `.venv` is missing deps.
  Run: `./.venv/bin/pip install -r requirements.txt` from the repo root.
- **`launchctl: Could not find specified service`** (after reboot) — the
  plist is in `~/Library/LaunchAgents/` but not loaded.
  `launchctl load ~/Library/LaunchAgents/com.devbrain.ingest.plist`.
- **Ingest not picking up sessions** — check `logs/ingest.err.log`, then
  verify your `ingest.project_mappings` contains an absolute-path prefix
  that matches where the source tool writes its JSONL.

---

## 8. Uninstall

To remove DevBrain cleanly:

```bash
# Stop & uninstall the launchd service (macOS)
launchctl unload ~/Library/LaunchAgents/com.devbrain.ingest.plist 2>/dev/null || true
rm -f ~/Library/LaunchAgents/com.devbrain.ingest.plist

# Stop & remove systemd unit (Linux, if you installed one)
sudo systemctl disable --now devbrain-ingest 2>/dev/null || true
sudo rm -f /etc/systemd/system/devbrain-ingest.service
sudo systemctl daemon-reload

# Stop the DB and delete its volume (destroys all DevBrain data)
cd /path/to/devbrain
docker compose down -v

# (Optional) delete Ollama models to reclaim disk
ollama rm snowflake-arctic-embed2
ollama rm qwen2.5:7b

# Delete the repo
cd .. && rm -rf devbrain
```

`.env`, `config/devbrain.yaml`, and `logs/` live inside the repo, so
deleting the directory removes all local config and logs.

---

## 9. Next steps

- **`ARCHITECTURE.md`** — how ingest, the MCP server, the dev
  factory, and Postgres fit together.
- **`docs/INSTANCE_PATTERN.md`** — conventions for running multiple
  DevBrain instances (per-user, per-project, or per-machine).

Once `devbrain doctor` is green and you have registered, point your
MCP-compatible client at `mcp-server/run.sh` and start using tools like
`get_project_context`, `deep_search`, and `store`.
