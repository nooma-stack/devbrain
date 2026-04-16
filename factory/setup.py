"""DevBrain interactive setup wizard.

Walks first-time users through GitHub auth, dev registration, project
configuration, notification channel setup, MCP client wiring, and
optional PKRelay installation. Generates config files and prints a
post-setup checklist of manual actions.

Called via: ./bin/devbrain setup
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import click
import yaml

from config import (
    CONFIG_PATH,
    DATABASE_URL,
    DEVBRAIN_HOME,
    load_config,
)
from state_machine import FactoryDB


# ─── Utilities ──────────────────────────────────────────────────────────────

def _header(title: str) -> None:
    click.echo()
    click.secho(f"━━━ {title} ", bold=True, nl=False)
    click.secho("━" * max(1, 56 - len(title)), bold=True)
    click.echo()


def _desc(*lines: str) -> None:
    for line in lines:
        click.secho(f"  {line}", dim=True)


def _ok(msg: str) -> None:
    click.echo(f"  {click.style('✓', fg='green')} {msg}")


def _info(msg: str) -> None:
    click.echo(f"  {click.style('→', fg='cyan')} {msg}")


def _warn(msg: str) -> None:
    click.echo(f"  {click.style('⚠', fg='yellow')} {msg}")


def _prompt(text: str, default: str = "", **kwargs) -> str:
    prefix = "  " if not text.startswith(" ") else ""
    return click.prompt(f"{prefix}{text}", default=default, **kwargs)


def _confirm(text: str, default: bool = True) -> bool:
    prefix = "  " if not text.startswith(" ") else ""
    return click.confirm(f"{prefix}{text}", default=default)


POST_ACTIONS: list[dict] = []


def _add_action(title: str, detail: str, condition: str = "") -> None:
    POST_ACTIONS.append({"title": title, "detail": detail, "condition": condition})


# ─── Config helpers ─────────────────────────────────────────────────────────

def _load_yaml() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


def _save_yaml(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def _append_env(key: str, value: str) -> None:
    env_path = DEVBRAIN_HOME / ".env"
    if env_path.exists():
        content = env_path.read_text()
        if f"{key}=" in content:
            return
    with open(env_path, "a") as f:
        f.write(f"\n{key}={value}\n")


# ─── Sections ──────────────────────────────────────────────────────────────

def setup_github() -> None:
    _header("GitHub Authentication")
    _desc(
        "The GitHub CLI (gh) is used by the dev factory to create branches,",
        "open pull requests, and push code. You can skip this if you only",
        "want DevBrain for memory (no factory pipeline).",
    )
    click.echo()

    if not shutil.which("gh"):
        _warn("GitHub CLI (gh) not found — skipping.")
        _warn("Install it: brew install gh")
        return

    result = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True)
    if result.returncode == 0:
        user_line = [l for l in result.stderr.splitlines() if "Logged in" in l]
        if user_line:
            _ok(f"Already authenticated: {user_line[0].strip()}")
        else:
            _ok("Already authenticated")
        return

    if _confirm("Authenticate with GitHub now? (opens browser)"):
        subprocess.run(["gh", "auth", "login"], check=False)
        _ok("GitHub authentication complete")
    else:
        _info("Skipped — run 'gh auth login' later to enable factory push/PR features.")


def setup_ai_cli_logins() -> None:
    _header("AI CLI Logins")
    _desc(
        "For any AI CLI installed on this system, you can log in now so",
        "DevBrain's factory can spawn them on your behalf. Each CLI uses",
        "its own subscription (Anthropic, OpenAI, Google Workspace).",
    )
    click.echo()

    clis = [
        {
            "name": "Claude Code",
            "cmd": "claude",
            "login_hint": "Run 'claude' — it opens a browser OAuth flow on first run",
            "desc": "Anthropic's CLI. Recommended for DevBrain's factory.",
            "check_args": ["claude", "--version"],
        },
        {
            "name": "Codex CLI",
            "cmd": "codex",
            "login_hint": "Run 'codex' — it opens a browser OAuth flow on first run",
            "desc": "OpenAI's CLI. Requires an OpenAI account with API access.",
            "check_args": ["codex", "--version"],
        },
        {
            "name": "Gemini CLI",
            "cmd": "gemini",
            "login_hint": "Run 'gemini' — it opens a browser OAuth flow on first run",
            "desc": "Google's CLI. Uses your Google/Workspace account.",
            "check_args": ["gemini", "--version"],
        },
    ]

    any_installed = False
    for cli in clis:
        if not shutil.which(cli["cmd"]):
            continue
        any_installed = True

        click.echo()
        click.secho(f"  {cli['name']}:", bold=True)
        _desc(cli["desc"])

        # Heuristic: run the CLI with --version (or equivalent) and check
        # if it succeeds. Most of these CLIs produce a version string
        # without needing authentication.
        try:
            result = subprocess.run(
                cli["check_args"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                _ok(f"{cli['name']} installed at {shutil.which(cli['cmd'])}")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        if _confirm(f"Launch {cli['name']} now to log in?", default=False):
            _info(f"Launching {cli['cmd']}... follow the browser prompts")
            _info("Come back to this terminal when login is done.")
            click.echo()
            # Run it interactively so OAuth flow works
            subprocess.run([cli["cmd"]], check=False)
            click.echo()
            _ok(f"{cli['name']} login flow complete")
        else:
            _info(f"Skipped. {cli['login_hint']}")

    if not any_installed:
        _info("No AI CLIs installed yet.")
        _info("Install one with DevBrain: run 'install-devbrain' and say yes at the AI CLI prompts.")
        _info("Or install manually:")
        _info("  Claude Code: curl -fsSL https://claude.ai/install.sh | bash")
        _info("  Codex:       npm install -g @openai/codex")
        _info("  Gemini:      npm install -g @google/gemini-cli")


def setup_identity() -> str:
    _header("Your Identity")
    _desc(
        "DevBrain tracks who submitted factory jobs and routes notifications",
        "to the right person. Your dev ID is typically your system username",
        "or GitHub handle.",
    )
    click.echo()

    default_id = os.environ.get("USER", "")
    dev_id = _prompt("Dev ID", default=default_id)
    full_name = _prompt("Full name", default="")

    db = FactoryDB(DATABASE_URL)
    existing = db.get_dev(dev_id)
    if existing:
        _ok(f"Dev '{dev_id}' already registered")
    else:
        db.register_dev(dev_id=dev_id, full_name=full_name or None, channels=[])
        _ok(f"Registered dev '{dev_id}'")

    return dev_id


def setup_projects() -> None:
    _header("Projects")
    _desc(
        "Projects tell DevBrain which codebases you work on. Each project",
        "gets its own memory space — decisions, patterns, and issues stay",
        "scoped to the right codebase. You can add more projects later.",
    )
    click.echo()

    cfg = _load_yaml()
    cfg.setdefault("ingest", {}).setdefault("project_mappings", {})
    cfg.setdefault("factory", {}).setdefault("project_paths", {})

    db = FactoryDB(DATABASE_URL)

    while True:
        if not _confirm("Add a project?", default=True):
            break

        click.echo()
        slug = _prompt("  Project slug (short, kebab-case)")
        name = _prompt("  Display name", default=slug)
        root_path = _prompt("  Source path (e.g., ~/code/myproject)")
        root_expanded = str(Path(root_path).expanduser())

        tech_stack_raw = _prompt("  Tech stack (comma-separated)", default="")
        tech_stack = [t.strip() for t in tech_stack_raw.split(",") if t.strip()] if tech_stack_raw else []

        lint_cmd = _prompt("  Lint command (or Enter to skip)", default="")
        test_cmd = _prompt("  Test command (or Enter to skip)", default="")

        constraints: list[str] = []
        if _confirm("  Add compliance/constraint rules?", default=False):
            click.echo()
            _desc("Enter constraints one per line. Empty line to finish.")
            _desc("Examples: 'No PHI in logs', 'All API calls through lib/client.ts'")
            while True:
                c = _prompt("    Constraint (Enter to finish)", default="")
                if not c:
                    break
                constraints.append(c)

        try:
            with db._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO devbrain.projects
                       (slug, name, root_path, description, constraints, tech_stack, lint_commands, test_commands)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (slug) DO UPDATE SET
                           name = EXCLUDED.name,
                           root_path = EXCLUDED.root_path,
                           tech_stack = EXCLUDED.tech_stack,
                           lint_commands = EXCLUDED.lint_commands,
                           test_commands = EXCLUDED.test_commands
                    """,
                    (
                        slug, name, root_expanded, f"{name} project",
                        json.dumps(constraints),
                        json.dumps({"stack": tech_stack}),
                        json.dumps({"lint": lint_cmd} if lint_cmd else {}),
                        json.dumps({"test": test_cmd} if test_cmd else {}),
                    ),
                )
                conn.commit()
            _ok(f"Project '{slug}' registered in database")
        except Exception as exc:
            _warn(f"DB insert failed: {exc}")

        cfg["ingest"]["project_mappings"][root_expanded] = slug
        cfg["factory"]["project_paths"][slug] = root_path
        _ok(f"Added ingest mapping: {root_path} → {slug}")

        click.echo()

    _save_yaml(cfg)
    _ok("Config saved to config/devbrain.yaml")


def setup_notifications(dev_id: str) -> None:
    _header("Notification Channels")
    _desc(
        "DevBrain can notify you when factory jobs complete, fail, get",
        "blocked, or need human attention. Choose which channels to enable.",
        "You can change these later in config/devbrain.yaml.",
    )
    click.echo()

    cfg = _load_yaml()
    cfg.setdefault("notifications", {}).setdefault("channels", {})
    cfg["notifications"].setdefault("notify_events", [
        "job_ready", "job_failed", "blocked", "needs_human",
    ])

    db = FactoryDB(DATABASE_URL)
    channels_to_register: list[dict] = []

    # tmux
    click.echo()
    _desc("tmux popup — Shows a notification popup in your terminal if you")
    _desc("are running inside a tmux session. Zero setup required.")
    if _confirm("Enable tmux notifications?", default=True):
        cfg["notifications"]["channels"]["tmux"] = {
            "enabled": True,
            "popup_width": 70,
            "popup_height": 20,
        }
        channels_to_register.append({"type": "tmux", "address": "popup"})
        _ok("tmux enabled")

    # Slack
    click.echo()
    _desc("Slack webhook — Posts to a Slack channel via an incoming webhook")
    _desc("URL. Create one at api.slack.com/messaging/webhooks. No bot needed.")
    if _confirm("Enable Slack notifications?", default=False):
        url = _prompt("  Webhook URL")
        cfg["notifications"]["channels"]["webhook_slack"] = {"enabled": True}
        _append_env("DEVBRAIN_SLACK_WEBHOOK_URL", url)
        channels_to_register.append({"type": "webhook_slack", "address": url})
        _ok("Slack webhook saved to .env")

    # Discord
    click.echo()
    _desc("Discord webhook — Posts to a Discord channel via a webhook URL.")
    _desc("Create one in Channel Settings → Integrations → Webhooks.")
    if _confirm("Enable Discord notifications?", default=False):
        url = _prompt("  Webhook URL")
        cfg["notifications"]["channels"]["webhook_discord"] = {"enabled": True}
        _append_env("DEVBRAIN_DISCORD_WEBHOOK_URL", url)
        channels_to_register.append({"type": "webhook_discord", "address": url})
        _ok("Discord webhook saved to .env")

    # Telegram
    click.echo()
    _desc("Telegram bot — Sends direct messages via a Telegram bot you")
    _desc("create. Create a bot with @BotFather, get the token, then")
    _desc("message your bot so DevBrain can discover your chat ID.")
    if _confirm("Enable Telegram notifications?", default=False):
        token = _prompt("  Bot token (from @BotFather)")
        bot_username = _prompt("  Bot username (without @)")
        cfg["notifications"]["channels"]["telegram_bot"] = {
            "enabled": True,
            "bot_username": bot_username,
        }
        _append_env("TELEGRAM_BOT_TOKEN", token)
        _ok("Telegram token saved to .env")
        _add_action(
            "Message your Telegram bot",
            f"Open Telegram, search for @{bot_username}, and send /start.\n"
            f"     Then run: ./bin/devbrain telegram-discover --username YOUR_HANDLE",
            condition="Telegram enabled",
        )

    # SMTP
    click.echo()
    _desc("Email (SMTP) — Sends notifications via any SMTP server (Gmail,")
    _desc("Outlook, SendGrid, self-hosted). Requires server credentials.")
    if _confirm("Enable email notifications?", default=False):
        host = _prompt("  SMTP host", default="smtp.gmail.com")
        port = _prompt("  SMTP port", default="587")
        sender = _prompt("  Sender email")
        password = _prompt("  SMTP password", hide_input=True)
        cfg["notifications"]["channels"]["smtp"] = {
            "enabled": True,
            "host": host,
            "port": int(port),
            "use_tls": True,
            "sender_email": sender,
            "sender_display_name": "DevBrain",
        }
        _append_env("DEVBRAIN_SMTP_PASSWORD", password)
        _ok("SMTP configured (password saved to .env)")

    # Register channels with the dev
    for ch in channels_to_register:
        try:
            db.add_dev_channel(dev_id, ch)
        except Exception:
            pass

    _save_yaml(cfg)
    _ok(f"Notification config saved ({len(channels_to_register)} channel(s) enabled)")


def setup_mcp_client() -> None:
    _header("MCP Client Configuration")
    _desc(
        "DevBrain exposes its tools via the Model Context Protocol (MCP).",
        "Your AI agent needs a one-time config snippet to connect. This",
        "step generates the right snippet for your agent of choice.",
    )
    click.echo()

    run_sh = DEVBRAIN_HOME / "mcp-server" / "run.sh"
    config_snippet = {
        "mcpServers": {
            "devbrain": {
                "command": str(run_sh),
            }
        }
    }

    agents = [
        ("Claude Code", "~/.claude/settings.json", "mcpServers"),
        ("Codex CLI", "~/.codex/config.json", "mcpServers"),
        ("Gemini CLI", "~/.gemini/settings.json", "mcpServers"),
    ]

    for agent_name, config_path, key in agents:
        _desc(f"{agent_name} — reads MCP server config from {config_path}.")
        if _confirm(f"Generate config for {agent_name}?", default=(agent_name == "Claude Code")):
            click.echo()
            click.echo(f"  Add this to {config_path}:")
            click.echo()
            formatted = json.dumps(config_snippet, indent=2)
            for line in formatted.splitlines():
                click.secho(f"    {line}", fg="cyan")
            click.echo()

            try:
                subprocess.run(
                    ["pbcopy"],
                    input=formatted.encode(),
                    check=True,
                    capture_output=True,
                )
                _ok("Copied to clipboard")
            except (FileNotFoundError, subprocess.CalledProcessError):
                _info("(pbcopy not available — copy manually from above)")

            _add_action(
                f"Add MCP config to {agent_name}",
                f"Paste the snippet above into {config_path}\n"
                f"     (already copied to clipboard if on macOS).",
                condition=f"{agent_name} selected",
            )

            _add_action(
                f"Restart {agent_name}",
                f"MCP config changes take effect on the next session start.",
                condition=f"{agent_name} selected",
            )
        click.echo()


def setup_pkrelay() -> None:
    _header("PKRelay Browser Extension (optional)")
    _desc(
        "PKRelay is a companion tool that gives your AI agents structured",
        "access to web browsers via MCP. Think of it as 'eyes and hands'",
        "for agents that need to see or interact with web pages.",
    )
    click.echo()
    _desc("What it does:")
    _desc("  • Captures page snapshots as structured data (not screenshots)")
    _desc("    — 10-50x more token-efficient than raw screenshots")
    _desc("  • Lets agents click buttons, fill forms, and navigate pages")
    _desc("  • Exposes browser state (tabs, URLs, console) via MCP tools")
    click.echo()
    _desc("Why it's useful with DevBrain:")
    _desc("  • Factory review agents can verify UI changes in a live browser")
    _desc("  • Research agents can browse docs and capture findings into memory")
    _desc("  • QA agents can run lightweight browser checks post-deployment")
    _desc("  • Everything captured flows into DevBrain as session context")
    click.echo()
    _desc("PKRelay is open-source (github.com/nooma-stack/pkrelay) and runs")
    _desc("as a Chrome extension + local MCP server. Not a required dependency.")
    click.echo()

    pkrelay_home = Path(os.environ.get("PKRELAY_HOME", Path.home() / "pkrelay"))

    if pkrelay_home.is_dir():
        _ok(f"PKRelay found at {pkrelay_home}")
        return

    if not _confirm("Install PKRelay?", default=False):
        _info("Skipped — install later from github.com/nooma-stack/pkrelay")
        return

    try:
        subprocess.run(
            ["git", "clone", "https://github.com/nooma-stack/pkrelay.git", str(pkrelay_home)],
            check=True,
        )
        _ok(f"Cloned to {pkrelay_home}")

        if (pkrelay_home / "install.sh").exists():
            subprocess.run(["bash", str(pkrelay_home / "install.sh")], check=True)
            _ok("PKRelay installed")
        elif (pkrelay_home / "package.json").exists():
            subprocess.run(["npm", "install", "--silent"], cwd=str(pkrelay_home), check=True)
            _ok("PKRelay dependencies installed")

        _add_action(
            "Load PKRelay in Chrome",
            f"Open chrome://extensions → Enable Developer Mode (top right)\n"
            f"     → Click 'Load unpacked' → Select {pkrelay_home}\n"
            f"     → Pin the extension for easy access.",
            condition="PKRelay installed",
        )
    except Exception as exc:
        _warn(f"PKRelay install failed: {exc}")
        _info("Install manually from github.com/nooma-stack/pkrelay")


def print_post_actions() -> None:
    if not POST_ACTIONS:
        return

    _header("Required Actions")
    _desc(
        "These steps need your manual attention. DevBrain is installed and",
        "configured, but these items can't be automated:",
    )
    click.echo()

    for i, action in enumerate(POST_ACTIONS, 1):
        click.echo(f"  {click.style(f'{i}.', bold=True)} {click.style(action['title'], bold=True)}")
        for line in action["detail"].splitlines():
            click.echo(f"     {line}")
        click.echo()

    click.echo(
        f"  After completing these, run {click.style('./bin/devbrain doctor', fg='cyan')}"
    )
    click.echo("  to verify everything is green.")


def run_verification() -> None:
    _header("Verification")
    _desc("Running devbrain doctor to confirm the installation...")
    click.echo()

    result = subprocess.run(
        [str(DEVBRAIN_HOME / "bin" / "devbrain"), "doctor"],
        capture_output=False,
    )

    if result.returncode == 0:
        click.echo()
        _ok("DevBrain is ready!")
    else:
        click.echo()
        _warn("Some checks failed — see above. Fix and re-run 'devbrain doctor'.")


# ─── Main entry point ──────────────────────────────────────────────────────

def run_setup() -> None:
    click.echo()
    click.secho("  DevBrain Setup Wizard", bold=True)
    click.secho("  Local-first persistent memory and dev factory for coding agents", dim=True)
    click.echo()
    click.echo("  This wizard walks you through first-time configuration.")
    click.echo("  Every setting can be changed later in config/devbrain.yaml")
    click.echo("  or by re-running this wizard.")
    click.echo()

    setup_github()
    setup_ai_cli_logins()
    dev_id = setup_identity()
    setup_projects()
    setup_notifications(dev_id)
    setup_mcp_client()
    setup_pkrelay()
    run_verification()
    print_post_actions()

    click.echo()
    click.secho("━" * 60, bold=True)
    click.echo()
    click.echo("  Setup complete. Run these to get started:")
    click.echo()
    click.secho("    ./bin/devbrain status    ", fg="cyan", nl=False)
    click.secho("— see factory job state", dim=True)
    click.secho("    ./bin/devbrain doctor    ", fg="cyan", nl=False)
    click.secho("— re-verify anytime", dim=True)
    click.secho("    ./bin/devbrain dashboard ", fg="cyan", nl=False)
    click.secho("— live factory TUI", dim=True)
    click.echo()
