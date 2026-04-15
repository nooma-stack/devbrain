# Instance Pattern

> **Status**: Documented pattern for v0.1. Operational multi-instance isolation (per-instance DB namespaces, multiple ingest services on one machine) is **not yet implemented** — see [Known Limitations](#known-limitations-for-v01). This document describes the architectural pattern so that organizations can start organizing their config around it today, and so that Phase 1+ work has a documented home to slot into.

---

## What is an instance?

DevBrain is a generic, open-source engine: a local-first memory store, ingest pipeline, MCP server, and dev factory. It has no opinions about your org, your projects, your compliance rules, or your naming conventions.

An **instance** is a downstream repository that wraps DevBrain with org-specific configuration and project context. The instance repo carries:

- An `instance.yaml` file that overrides the generic `devbrain/config/devbrain.yaml`
- Project-specific context (READMEs, compliance notes, coding standards)
- The instance's own `.env` with local secrets
- Optional custom hooks or adapters

DevBrain itself lives inside the instance as a **git submodule**. Upstream improvements to the engine arrive via `git submodule update --remote`; your instance config and project context stay put.

**Why the separation exists:**

- **The engine stays generic.** DevBrain's repo contains no Acme-Corp-specific code, no HIPAA rule tables, no hardcoded project names. Anyone can clone and use it.
- **Your org customizes without forking.** Forks drift. Submodules pull cleanly. If you fork DevBrain to add your compliance rules, you will hit merge conflicts on every upstream release. The instance pattern avoids that entirely.
- **Teams can share an instance.** Your `acme-instance` repo is the thing your team clones and contributes to. DevBrain is a dependency, not a project you maintain.
- **Version pinning is explicit.** The submodule commit in your instance repo pins the DevBrain version you tested against. You upgrade deliberately.

---

## When to create an instance vs. running DevBrain directly

Not everyone needs an instance. The pattern is overhead. Use it only when the benefits outweigh the setup cost.

### Use DevBrain directly

Clone `nooma-stack/devbrain`, edit `config/devbrain.yaml`, and run. No instance repo required.

Good fit when:

- **Personal use.** One developer, one machine, no team to share with.
- **Single-user.** No one else edits your config.
- **Generic project memory.** You want persistent memory across your coding sessions but have no org-specific rules to encode.
- **Exploring DevBrain.** You are evaluating whether it fits your workflow before committing to a pattern.

### Create an instance

Bootstrap an instance repo (`my-org-instance`) with DevBrain as a submodule.

Good fit when:

- **Org-specific projects.** You have a defined set of projects with names, tech stacks, lint commands, and owners that belong together.
- **Compliance rules.** Your projects carry HIPAA, FERPA, SOC2, PCI-DSS, or similar constraints that should travel with the config.
- **Shared with a team.** Multiple developers clone the same instance and expect consistent behavior.
- **Version pinning matters.** You want DevBrain at a known commit and upgrade on your schedule.
- **Instance-specific notification routing.** Your org wants Slack alerts for one team, email for another, Telegram for on-call.
- **Different model preferences.** Your org standardizes on a specific embedding or summary model different from DevBrain defaults.

If you are unsure, start direct. You can migrate to an instance later without data loss.

---

## The submodule + YAML pattern

Canonical structure for an instance repository:

```
my-org-instance/
├── devbrain/                    ← git submodule (the engine)
├── instance.yaml                ← instance-specific config overrides
├── .env                         ← local secrets (gitignored)
├── .env.example                 ← committed template for onboarding
├── projects/                    ← optional: instance-specific project context
│   ├── project1/
│   │   └── README.md
│   └── project2/
│       ├── README.md
│       └── compliance.md
├── hooks/                       ← optional: instance custom hooks
│   └── session-start.sh
├── .gitignore                   ← ignore .env, devbrain/config/devbrain.yaml
└── README.md                    ← instance-specific setup and onboarding
```

Key points:

- **`devbrain/` is a submodule**, not a copy. It is pinned to a specific upstream commit.
- **`instance.yaml` is the only config file the instance owns.** It overrides `devbrain/config/devbrain.yaml` field-by-field.
- **`.env` never gets committed.** Real secrets stay local. `.env.example` is the committed template.
- **`projects/` is optional** but highly recommended. It gives you a place for per-project READMEs, compliance notes, and coding standards the engine can surface as context.
- **`hooks/` is optional.** If your org needs custom session-start behavior, put it here and point DevBrain at it via config.

### Config merge semantics

DevBrain's config loader applies the following precedence (highest wins):

1. **Environment variables** (`DEVBRAIN_*` in `.env` or shell)
2. **`instance.yaml`** (instance-level overrides)
3. **`devbrain/config/devbrain.yaml`** (engine defaults, if present)
4. **Built-in defaults** (hardcoded in the engine)

Fields in `instance.yaml` are merged on top of the engine config — you only specify what you want to override. Leave everything else alone and the engine defaults apply.

---

## `instance.yaml` schema

A concrete example for a fictional `acme-instance`:

```yaml
# acme-instance/instance.yaml

instance:
  name: acme-instance
  owner: platform-team@acme.example
  description: |
    Acme Corp internal dev factory. Covers the customer-portal,
    billing-api, and data-platform projects.

# Projects this instance knows about. Supplements the engine's
# ingest.project_mappings and factory.project_paths.
projects:
  - slug: customer-portal
    name: Customer Portal
    root_path: ~/code/acme/customer-portal
    tech_stack: [typescript, nextjs, postgres]
    constraints:
      - "Frontend-only changes must not modify shared/types/*"
      - "All API calls go through lib/api-client.ts"
    lint_commands:
      - "npm run lint"
      - "npm run typecheck"
    test_commands:
      - "npm run test"

  - slug: billing-api
    name: Billing API
    root_path: ~/code/acme/billing-api
    tech_stack: [python, fastapi, postgres]
    compliance: [pci-dss]
    lint_commands:
      - "ruff check ."
      - "black --check ."
    test_commands:
      - "pytest"

  - slug: data-platform
    name: Data Platform
    root_path: ~/code/acme/data-platform
    tech_stack: [python, airflow, snowflake]
    compliance: [soc2]
    lint_commands:
      - "ruff check ."
    test_commands:
      - "pytest tests/unit"

# Compliance rules that apply to any project tagged with the matching label.
compliance:
  pci-dss:
    forbid_patterns:
      - "log.*card_number"
      - "print.*cvv"
    require_review: security
    retention_days: 365

  soc2:
    require_review: security
    audit_log: true

# Instance-specific notification routing. Overrides
# devbrain.notifications.channels entirely.
notifications:
  notify_events:
    - job_ready
    - job_failed
    - blocked
    - needs_human
  channels:
    webhook_slack:
      enabled: true
      # URL comes from .env via DEVBRAIN_SLACK_WEBHOOK_URL
    smtp:
      enabled: true
      sender_email: devbrain@acme.example
      sender_display_name: "Acme DevBrain"

# Model preferences — override if the org standardizes on different models.
models:
  embedding: snowflake-arctic-embed2
  summary: qwen2.5:14b           # org prefers larger summary model

# Optional: point DevBrain at instance-local hooks.
hooks:
  session_start: ./hooks/session-start.sh
```

Every field is optional. A minimal `instance.yaml` is as short as:

```yaml
instance:
  name: my-org-instance
  owner: me@example.com
projects:
  - slug: myproject
    root_path: ~/code/myproject
```

---

## Bootstrapping a new instance

Step-by-step to create a fresh instance repo. This is a manual workflow for v0.1 — a dedicated `devbrain instance init` command is planned for Phase 1+ (see [Roadmap](#roadmap-for-the-instance-pattern)).

```bash
# 1. Create the instance directory and init git
mkdir my-org-instance && cd my-org-instance
git init

# 2. Add DevBrain as a submodule
git submodule add https://github.com/nooma-stack/devbrain.git devbrain

# 3. Copy config templates into the instance root
cp devbrain/config/devbrain.yaml.example instance.yaml
cp devbrain/.env.example .env

# 4. Edit instance.yaml — replace with your projects, compliance, notifications
$EDITOR instance.yaml

# 5. Edit .env — set DEVBRAIN_HOME, DEVBRAIN_DATABASE_URL, secrets
$EDITOR .env

# 6. Gitignore the local secrets
echo ".env" >> .gitignore
echo "devbrain/config/devbrain.yaml" >> .gitignore

# 7. Install and verify the engine
cd devbrain
./scripts/install-ingest-service.sh
./bin/devbrain doctor

# 8. Commit the instance skeleton
cd ..
git add .
git commit -m "chore: bootstrap instance"
```

After bootstrap, day-to-day work happens from the instance root. Point DevBrain's config loader at `instance.yaml` via `DEVBRAIN_CONFIG=/path/to/my-org-instance/instance.yaml` in your `.env`, or let the loader pick it up automatically if it lives one level above the `devbrain/` submodule (planned behavior, not fully wired in v0.1 — see limitations below).

---

## Upgrading the engine

DevBrain releases happen upstream. Instances pull them in via submodule update.

```bash
cd my-org-instance

# 1. Pull the latest DevBrain commit (or pin to a specific tag)
cd devbrain
git fetch --tags
git checkout v0.2.0        # or: git pull origin main
cd ..

# 2. Run any new migrations
cd devbrain
./bin/devbrain migrate
cd ..

# 3. Verify the install still passes
./devbrain/bin/devbrain doctor

# 4. Commit the new submodule pointer
git add devbrain
git commit -m "chore: bump devbrain to v0.2.0"
```

If `doctor` reports new required env vars or config fields after an upgrade, check the upstream changelog and update your `instance.yaml` or `.env` accordingly.

---

## Known limitations for v0.1

Be honest about what is documented versus what is implemented. The instance pattern is a **conceptual architecture** in v0.1. Several pieces you would expect from a multi-instance system do not exist yet.

### Single DB namespace

All instances on a single machine share the same Postgres database and the same `devbrain.*` schema. There is no per-instance schema, no per-instance table prefix, no per-instance database. If you run two instances on one machine, their memory pools merge into one.

**Workaround for v0.1**: plan for **one instance per machine**. If you need two isolated memory pools, run them on separate machines.

### Single launchd / ingest service

The install script (`scripts/install-ingest-service.sh`) registers one launchd service on macOS. Running multiple instances on one machine would require multiple services with distinct labels, working directories, and watched paths. That plumbing does not exist.

**Workaround for v0.1**: one ingest service per machine.

### No formal `devbrain instance init` command

Bootstrapping an instance is a manual checklist (see above). There is no CLI command that scaffolds the directory structure, prompts for config values, or validates the result.

**Workaround for v0.1**: follow the bootstrap steps manually. Consider committing a template instance repo your team copies from.

### Config loader does not auto-discover `instance.yaml`

DevBrain's config loader reads `config/devbrain.yaml` inside the submodule by default. To point it at your instance's `instance.yaml`, set `DEVBRAIN_CONFIG=/path/to/my-org-instance/instance.yaml` in `.env`. Auto-discovery of `../instance.yaml` relative to the submodule is planned but not implemented.

### Cross-instance memory is not exposed

Because only one DB exists today, the question "can instance A query instance B's memory?" is moot — there is only one memory pool. When DB-per-instance lands in Phase 1+, we will need explicit tooling for cross-instance queries. None exists yet.

### Instance-level hooks are best-effort

`instance.yaml` can reference custom hooks under `hooks/`, but the hook resolution logic in the engine still assumes hooks live inside the submodule. Until the hook loader is instance-aware, the safest approach is to place custom hooks inside `devbrain/hooks/` (which will be clobbered on submodule update) or wait for Phase 1+.

---

## Roadmap for the instance pattern

Phase 1+ work to make the instance pattern fully operational:

- **DB namespace per instance.** Each instance gets its own schema (`devbrain_<instance_slug>.*`) or its own database. Config loader wires the right connection string per instance.
- **Multiple ingest services per machine.** Install script accepts an instance name and registers a uniquely-labeled launchd (or systemd) service per instance.
- **`devbrain instance init` command.** Scaffolds the instance directory, adds the submodule, copies templates, prompts for core values, runs `doctor`.
- **`devbrain instance list` / `devbrain instance switch`.** For developers who operate across multiple instances on one machine.
- **Instance-aware MCP tool scoping.** MCP tools default to the current instance's DB but can be explicitly scoped to another instance when cross-instance queries are needed.
- **Auto-discovery of `instance.yaml`.** Config loader walks up from the submodule root to find `../instance.yaml` without needing `DEVBRAIN_CONFIG`.
- **Instance-aware hook resolution.** Hooks in the instance repo take precedence over engine defaults.
- **Upgrade compatibility checks.** `devbrain doctor` after a submodule bump reports any `instance.yaml` fields that are now deprecated or renamed.

None of this is v0.1 scope. The point of v0.1 is to lock down the engine as a clean dependency so Phase 1+ can build the instance tooling on firm ground.

---

## Example

A minimal working instance lives at `examples/instance-example/` in the DevBrain repo. It contains:

- `instance.yaml` — a minimal viable config with one project, one compliance rule, and one notification channel
- `README.md` — walkthrough of what each field does and how to copy this into your own instance repo

Start there when you are ready to create your own instance. Copy the directory out of `examples/`, rename it, and follow the bootstrapping steps above.

---

## FAQ

**Should I fork DevBrain instead of using a submodule?**
No. Forks diverge and you will hit merge conflicts on every upstream release. A submodule lets you pull upstream improvements cleanly while keeping your instance config separate. The whole point of the instance pattern is to avoid forking.

**Can I use DevBrain without creating an instance?**
Yes. The instance pattern is optional. For personal use, single-user setups, or evaluation, clone DevBrain directly and edit `config/devbrain.yaml`. You can migrate to an instance later if your needs grow.

**Can one machine host multiple instances?**
Not in v0.1. Multi-instance operational isolation (separate DB namespaces, multiple ingest services) is Phase 1+ work. For now, plan on one instance per machine. If you need two isolated memory pools, use two machines.

**What happens to my `instance.yaml` when I upgrade DevBrain?**
Nothing — it lives in your instance repo, not in the submodule. Submodule updates only change the contents of `devbrain/`. Your `instance.yaml`, `.env`, `projects/`, and `hooks/` are untouched.

**Can I commit `instance.yaml` to a public repo?**
Yes, as long as you have no secrets in it. Secrets go in `.env`, which is gitignored. `instance.yaml` is meant to be committed so your team shares the same project list, compliance rules, and routing.

**What if my org has multiple independent teams?**
Each team can maintain its own instance repo. They may share a submodule pin or drift independently. Once per-instance DB isolation lands in Phase 1+, teams can run their instances side by side on shared infrastructure.

**Does the engine know it is running inside an instance?**
Weakly, in v0.1. The engine reads whatever config file it is pointed at. Phase 1+ will add explicit instance-awareness (the engine knows its instance name, scopes its DB namespace, reports it in `doctor` output).
