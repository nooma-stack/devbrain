# Example DevBrain Instance

This directory is a **minimal viable instance** — a template you can copy,
rename, and fill in to create your own DevBrain instance.

See [`docs/INSTANCE_PATTERN.md`](../../docs/INSTANCE_PATTERN.md) for the full
conceptual explanation. This README shows what a small real instance looks
like.

---

## What's in this directory

```
instance-example/
├── README.md           ← you are here
├── instance.yaml       ← instance-specific config (projects, notifications, models)
└── .env.example        ← environment overrides (copy to .env; .env is gitignored)
```

A real instance repo would also contain:

```
my-instance/
├── devbrain/           ← DevBrain engine as a git submodule
├── instance.yaml       ← copied from this example
├── .env                ← copied from .env.example, secrets filled in
└── README.md           ← your own instance docs
```

---

## Using this template

From the repo where you want your instance to live:

```bash
# Create a new instance repo
mkdir my-instance && cd my-instance
git init

# Add DevBrain as a submodule (pick a version tag for reproducibility)
git submodule add https://github.com/nooma-stack/devbrain.git devbrain
cd devbrain && git checkout v0.1.0 && cd ..

# Seed your instance from this example
cp devbrain/examples/instance-example/instance.yaml instance.yaml
cp devbrain/examples/instance-example/.env.example .env

# Edit instance.yaml — replace "example" with your real project info
# Edit .env — fill in DEVBRAIN_DATABASE_URL and any notification secrets

# Run DevBrain's installer (macOS) or follow devbrain/INSTALL.md
./devbrain/scripts/install-ingest-service.sh

# Verify
./devbrain/bin/devbrain doctor
```

---

## What `instance.yaml` does

When DevBrain starts, it loads configuration in this precedence order:

1. Environment variables (from `.env`, highest priority)
2. `instance.yaml` (instance-specific overrides)
3. `devbrain/config/devbrain.yaml` (engine defaults)
4. Built-in fallback defaults

Fields in `instance.yaml` are **merged on top of** the engine config — you
only specify what you want to override. Leave anything else out and
DevBrain uses its defaults.

> **v0.1 caveat:** DevBrain's config loader currently reads from
> `devbrain/config/devbrain.yaml`. Until the instance-aware loader lands
> in Phase 1, symlink or copy your `instance.yaml` to
> `devbrain/config/devbrain.yaml`, or set `DEVBRAIN_CONFIG=../instance.yaml`
> in `.env`. See `docs/INSTANCE_PATTERN.md` for the full Phase 1 roadmap.

---

## Customizing the template

### Adding a project

Each project you want DevBrain to know about needs a minimal entry:

```yaml
projects:
  - slug: your-project-slug
    name: Your Project Name
    root_path: ~/code/your-project
    tech_stack: [python, fastapi]
    lint_commands:
      - "ruff check ."
    test_commands:
      - "pytest"
```

The **slug** is how DevBrain identifies the project internally. Use
kebab-case and keep it short.

### Wiring up notifications

The example comes with notifications disabled. Enable channels by setting
`enabled: true` and filling in the required fields. Secrets (webhook URLs,
SMTP passwords, bot tokens) should come from `.env`, not be committed:

```yaml
notifications:
  channels:
    webhook_slack:
      enabled: true
      # URL read from DEVBRAIN_SLACK_WEBHOOK_URL in .env
```

```bash
# .env
DEVBRAIN_SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
```

### Using different models

If your org runs larger local models, override them:

```yaml
models:
  embedding: snowflake-arctic-embed2
  summary: qwen2.5:14b       # bigger summary model than the default 7b
```

Make sure the models are pulled in Ollama on every machine that runs your
instance (`ollama pull qwen2.5:14b`).

---

## Where to go next

- **Full instance pattern documentation:** [`docs/INSTANCE_PATTERN.md`](../../docs/INSTANCE_PATTERN.md)
- **DevBrain install guide:** [`INSTALL.md`](../../INSTALL.md)
- **DevBrain architecture:** [`ARCHITECTURE.md`](../../ARCHITECTURE.md)
