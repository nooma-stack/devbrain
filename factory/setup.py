"""DevBrain interactive setup wizard.

Walks first-time users through GitHub auth, dev registration, project
configuration, notification channel setup, MCP client wiring, and
optional PKRelay installation. Generates config files and prints a
post-setup checklist of manual actions.

Called via: ./bin/devbrain setup
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import click
import yaml


def _ensure_tty_stdin() -> None:
    """Force sys.stdin to the controlling terminal so Click prompts work.

    When `devbrain setup` is spawned as a subprocess from install.sh
    (which itself was invoked via curl|bash and later exec'd with its
    stdin redirected to /dev/tty), the inherited stdin sometimes ends
    up in a state where Click reads EOF immediately and aborts without
    user input. Explicitly re-opening /dev/tty here guarantees Click
    has a live terminal to read from regardless of how the script was
    invoked.
    """
    try:
        tty_fd = os.open("/dev/tty", os.O_RDWR)
    except OSError:
        return  # No TTY available (CI, piped input) — leave stdin as-is
    try:
        # Wrap as text streams with line buffering so prompts get flushed
        sys.stdin = os.fdopen(tty_fd, "r", buffering=1)
    except Exception:
        os.close(tty_fd)

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
    """Upsert KEY=value into .env with 0600 permissions.

    - Existing KEY= lines are replaced (re-running setup updates keys).
    - File is created with mode 0600 (owner read/write only) so secrets
      aren't world-readable on shared systems.
    - Empty lines preserved; comments preserved.
    """
    env_path = DEVBRAIN_HOME / ".env"
    lines: list[str] = []
    key_updated = False

    if env_path.exists():
        lines = env_path.read_text().splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(f"{key}=") or stripped.startswith(f"export {key}="):
                lines[i] = f"{key}={value}"
                key_updated = True
                break

    if not key_updated:
        if lines and lines[-1]:
            lines.append("")  # blank line before new entry
        lines.append(f"{key}={value}")

    env_path.write_text("\n".join(lines) + "\n")
    env_path.chmod(0o600)


_SECURITY_NOTE_SHOWN = False


def _show_env_security_note() -> None:
    """Print a one-time explainer about how .env stores secrets."""
    global _SECURITY_NOTE_SHOWN
    if _SECURITY_NOTE_SHOWN:
        return
    _SECURITY_NOTE_SHOWN = True
    click.echo()
    _info("About .env security:")
    _desc(f"  • Location: {DEVBRAIN_HOME}/.env")
    _desc("  • Permissions: 0600 (owner read/write only)")
    _desc("  • Git-ignored — won't be committed")
    _desc("  • Loaded by bin/devbrain and mcp-server/run.sh into env vars")
    _desc("  • Visible in `ps auxe` for processes you launch (OS standard)")
    _desc("  • Rotate keys anytime: edit .env directly or re-run 'devbrain setup'")


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
    _header("AI CLI Auth")
    _desc(
        "For any AI CLI installed on this system, you can authenticate now",
        "so DevBrain's factory can spawn them on your behalf. Two options",
        "per CLI:",
        "",
        "  OAuth (subscription)  — Claude Max/Pro, ChatGPT Pro/Plus, Google",
        "                          account. Opens a browser to log in.",
        "  API key               — Pay-as-you-go billing. Key stored in .env",
        "                          (gitignored) and loaded as an env var",
        "                          the CLI picks up automatically.",
    )
    click.echo()

    clis = [
        {
            "name": "Claude Code",
            "cmd": "claude",
            "desc": "Anthropic's CLI. Recommended for DevBrain's factory.",
            "env_var": "ANTHROPIC_API_KEY",
            "key_url": "https://console.anthropic.com/settings/keys",
        },
        {
            "name": "Codex CLI",
            "cmd": "codex",
            "desc": "OpenAI's CLI. Works with ChatGPT subscription or OpenAI API.",
            "env_var": "OPENAI_API_KEY",
            "key_url": "https://platform.openai.com/api-keys",
        },
        {
            "name": "Gemini CLI",
            "cmd": "gemini",
            "desc": "Google's CLI. Works with Google account or API key.",
            "env_var": "GEMINI_API_KEY",
            "key_url": "https://aistudio.google.com/apikey",
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
        _ok(f"Installed at {shutil.which(cli['cmd'])}")

        # Show three-way choice
        click.echo()
        click.echo(f"    1. OAuth — opens browser for subscription login")
        click.echo(f"    2. API key — paste a {cli['env_var']} for pay-as-you-go billing")
        click.echo(f"    3. Skip (configure later)")
        click.echo()

        choice = _prompt(f"Auth method for {cli['name']} (1/2/3)", default="3").strip()

        if choice == "1":
            click.echo()
            _info(f"Launching {cli['name']} CLI for OAuth login.")
            _warn("IMPORTANT — after the browser login completes:")
            _desc("  • Complete the OAuth flow in your browser")
            _desc("  • The CLI may enter interactive mode (a chat prompt)")
            _desc("  • Type '/quit' or '/exit' (or press Ctrl+C twice)")
            _desc("    to exit the CLI and return to this setup wizard")
            click.echo()
            _info(f"Press Enter to launch {cli['cmd']}...")
            try:
                input()  # Pause so user reads the instructions
            except (EOFError, KeyboardInterrupt):
                _info("Skipped.")
                continue
            subprocess.run([cli["cmd"]], check=False)
            click.echo()
            _ok(f"{cli['name']} login flow complete (token stored by CLI)")
        elif choice == "2":
            _info(f"Get an API key from: {cli['key_url']}")
            click.echo()
            key = _prompt(f"    {cli['env_var']}", hide_input=True, default="").strip()
            if key:
                _append_env(cli["env_var"], key)
                _ok(f"{cli['env_var']} saved to .env (mode 0600)")
                _info(f"The CLI will pick this up automatically on next run.")
                _show_env_security_note()
            else:
                _warn("Empty key — skipped.")
        else:
            _info(f"Skipped. Configure later by either:")
            _info(f"  • Running '{cli['cmd']}' for OAuth login")
            _info(f"  • Adding {cli['env_var']}=sk-... to .env for API key")

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
    _desc("Telegram bot — Sends notifications to your Telegram account via")
    _desc("a bot you create just for yourself (not public). Two-step setup:")
    _desc("  create the bot with @BotFather, then send /start to it so DevBrain")
    _desc("  can discover your chat ID.")
    if _confirm("Enable Telegram notifications?", default=False):
        click.echo()
        _info("How to create a bot (takes ~1 minute):")
        _desc("  1. Open Telegram (app or web.telegram.org)")
        _desc("  2. Search for @BotFather (official blue-check bot)")
        _desc("  3. Send:  /newbot")
        _desc("  4. BotFather asks for a display name — type any name")
        _desc("     (e.g., 'DevBrain Alice')")
        _desc("  5. BotFather asks for a username — must end in 'bot'")
        _desc("     and be globally unique (e.g., 'alice_devbrain_bot')")
        _desc("  6. BotFather replies with a token like '1234567890:AAE...xyz'")
        _desc("     Copy just the token (everything after the number:colon)")
        click.echo()
        token = _prompt("  Paste the bot token", hide_input=True)
        bot_username = _prompt("  Bot username (without @)")
        cfg["notifications"]["channels"]["telegram_bot"] = {
            "enabled": True,
            "bot_username": bot_username,
        }
        _append_env("TELEGRAM_BOT_TOKEN", token)
        _ok("Telegram token saved to .env (mode 0600)")
        _show_env_security_note()

        click.echo()
        _info(f"Next steps (do these before testing):")
        _desc(f"  7. In Telegram, search for @{bot_username}")
        _desc(f"  8. Send /start to your bot (just type /start and send)")
        _desc(f"  9. Back in this terminal (after setup finishes), run:")
        _desc(f"     ./bin/devbrain telegram-discover --username YOUR_TELEGRAM_HANDLE")
        _desc(f"     (that's your @username on Telegram, not the bot's)")
        _desc(f"  10. That command completes the pairing and sends a test message.")
        _add_action(
            "Message your Telegram bot and pair DevBrain to your chat",
            f"Open Telegram, search for @{bot_username}, send /start.\n"
            f"     Then run: ./bin/devbrain telegram-discover --username YOUR_HANDLE\n"
            f"     (YOUR_HANDLE is your personal @ on Telegram, not the bot's name)",
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


def _merge_mcp_into_json(config_path: Path, devbrain_entry: dict) -> tuple[str, bool]:
    """Auto-merge DevBrain's MCP config into an AI CLI's JSON config file.

    Creates the file + parent directory if missing. Preserves all other
    config keys and other MCP servers. If 'devbrain' is already configured
    with the same command, it's a no-op; if configured with a different
    command, we update it (so re-running setup after moving DEVBRAIN_HOME
    does the right thing).

    Returns (message, success). success=False means we didn't write
    (e.g., invalid existing JSON — user must fix manually).
    """
    config_path = config_path.expanduser()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if config_path.exists() and config_path.stat().st_size > 0:
        try:
            with open(config_path) as f:
                existing = json.load(f)
            if not isinstance(existing, dict):
                return (f"{config_path} is JSON but not an object — refusing to overwrite", False)
        except json.JSONDecodeError as e:
            return (f"{config_path} has invalid JSON ({e.msg}) — refusing to overwrite. "
                    f"Fix the file or delete it and re-run 'devbrain setup'.", False)

    existing.setdefault("mcpServers", {})
    if not isinstance(existing["mcpServers"], dict):
        return (f"{config_path} has a non-dict 'mcpServers' — refusing to overwrite", False)

    previous = existing["mcpServers"].get("devbrain")
    existing["mcpServers"]["devbrain"] = devbrain_entry

    with open(config_path, "w") as f:
        json.dump(existing, f, indent=2)
        f.write("\n")

    if previous is None:
        return (f"Added devbrain MCP server to {config_path}", True)
    elif previous == devbrain_entry:
        return (f"devbrain MCP server already configured in {config_path} (no change)", True)
    else:
        return (f"Updated existing devbrain MCP server in {config_path}", True)


def setup_mcp_client() -> None:
    _header("MCP Client Configuration")
    _desc(
        "DevBrain exposes its tools via the Model Context Protocol (MCP).",
        "For each AI CLI you have installed, we'll auto-merge DevBrain's",
        "MCP server config into the CLI's JSON config file — creating the",
        "file if it doesn't exist, preserving any existing config.",
    )
    click.echo()

    run_sh = DEVBRAIN_HOME / "mcp-server" / "run.sh"
    devbrain_entry = {"command": str(run_sh)}

    # (display name, config path, command to detect if CLI is installed)
    agents = [
        ("Claude Code", "~/.claude/settings.json", "claude"),
        ("Codex CLI", "~/.codex/config.json", "codex"),
        ("Gemini CLI", "~/.gemini/settings.json", "gemini"),
    ]

    any_configured = False
    for agent_name, config_path_str, cli_cmd in agents:
        if not shutil.which(cli_cmd):
            _info(f"{agent_name}: CLI not installed — skipping MCP config")
            click.echo()
            continue

        config_path = Path(config_path_str).expanduser()
        _desc(f"{agent_name} — config file: {config_path}")

        if not _confirm(f"Auto-configure MCP for {agent_name}?", default=True):
            click.echo()
            continue

        message, success = _merge_mcp_into_json(config_path, devbrain_entry)
        if success:
            _ok(message)
            any_configured = True
            _add_action(
                f"Restart {agent_name}",
                f"{agent_name} picks up MCP config changes on its next session start.\n"
                f"     After restart, run '/mcp' inside {agent_name} to verify DevBrain tools are available.",
                condition=f"{agent_name} configured",
            )
        else:
            _warn(message)
            # Fall back to manual-paste flow
            _info("Showing config snippet for manual paste:")
            click.echo()
            manual_snippet = {"mcpServers": {"devbrain": devbrain_entry}}
            formatted = json.dumps(manual_snippet, indent=2)
            for line in formatted.splitlines():
                click.secho(f"    {line}", fg="cyan")
            click.echo()
            try:
                subprocess.run(
                    ["pbcopy"], input=formatted.encode(),
                    check=True, capture_output=True,
                )
                _ok("Copied to clipboard")
            except (FileNotFoundError, subprocess.CalledProcessError):
                _info("(pbcopy not available — copy manually from above)")
            _add_action(
                f"Manually add MCP config to {agent_name}",
                f"Merge this into {config_path} (clipboard has the snippet):\n"
                f"     {formatted.replace(chr(10), chr(10) + '     ')}",
                condition=f"{agent_name} needs manual MCP config",
            )
        click.echo()

    if not any_configured:
        _info("No AI CLIs configured. Install one (e.g., Claude Code via the")
        _info("native installer) and re-run 'devbrain setup' to wire it up.")


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

# ─── Menu / dispatch ────────────────────────────────────────────────────────

def _resolve_dev_id_for_section() -> str:
    """Return the dev_id for sections that need one (e.g., notifications).
    Uses existing registration if present, otherwise $USER as default.
    Does not prompt — sections that need actual registration should call
    setup_identity() first."""
    import os
    db = FactoryDB(DATABASE_URL)
    candidate = os.environ.get("USER", "")
    existing = db.get_dev(candidate) if candidate else None
    if existing:
        return candidate
    _warn("No dev identity registered yet.")
    _info("Running identity setup first (required for this section)...")
    return setup_identity()


def _run_channels_section() -> None:
    """Wrapper for setup_notifications that resolves dev_id first."""
    dev_id = _resolve_dev_id_for_section()
    setup_notifications(dev_id)


# (menu label, section key, runner callable)
MENU_SECTIONS: list[tuple[str, str, callable]] = [
    ("Full setup (run every section in order)", "full", None),  # special-cased
    ("GitHub authentication",                  "github",   setup_github),
    ("AI CLI authentication (Claude / Codex / Gemini, OAuth or API key)", "ai-clis", setup_ai_cli_logins),
    ("Dev identity (register or update)",      "identity", setup_identity),
    ("Projects (register new or update)",      "projects", setup_projects),
    ("Notification channels (tmux, Slack, Telegram, SMTP, etc.)", "channels", _run_channels_section),
    ("MCP client config (Claude Code, Codex, Gemini)", "mcp", setup_mcp_client),
    ("PKRelay browser extension (optional)",   "pkrelay",  setup_pkrelay),
    ("Verify installation (run devbrain doctor)", "verify", run_verification),
    ("Show post-setup required actions",       "actions",  print_post_actions),
    ("Exit",                                    "exit",    None),  # special-cased
]


def _run_full_setup() -> None:
    """The linear, first-time-user flow: every section in order."""
    click.echo()
    click.secho("  Running full setup — every section in order.", bold=True)
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


def _show_menu() -> None:
    """Print the menu of available sections."""
    click.echo()
    click.secho("  What would you like to do?", bold=True)
    click.echo()
    for i, (label, _, _) in enumerate(MENU_SECTIONS, 1):
        click.echo(f"    {i:>2}. {label}")
    click.echo()


def _run_menu_loop() -> None:
    """Interactive menu: pick a section, run it, return to menu until exit."""
    while True:
        _show_menu()
        choice_raw = _prompt(f"Choose (1-{len(MENU_SECTIONS)})", default="1")
        try:
            idx = int(choice_raw) - 1
            if not 0 <= idx < len(MENU_SECTIONS):
                raise ValueError()
        except ValueError:
            _warn(f"Invalid choice: '{choice_raw}'. Enter a number 1-{len(MENU_SECTIONS)}.")
            continue

        label, section_key, runner = MENU_SECTIONS[idx]

        if section_key == "exit":
            click.echo()
            _info("Exiting setup. Run 'devbrain setup' anytime to return.")
            return
        if section_key == "full":
            _run_full_setup()
            return  # full flow is terminal

        click.echo()
        try:
            runner()
        except click.exceptions.Abort:
            click.echo()
            _warn("Section interrupted.")

        click.echo()
        if not _confirm("Return to menu?", default=True):
            click.echo()
            print_post_actions()
            return


def run_setup(section: str | None = None) -> None:
    """Entry point for 'devbrain setup'.

    Behavior:
      - no argument       → interactive menu (recommended for most users)
      - section=<key>     → jump directly to that section, then exit
      - section='full'    → run all sections linearly (first-time-user flow)

    Valid section keys: github, ai-clis, identity, projects, channels,
    mcp, pkrelay, verify, actions, full.
    """
    _ensure_tty_stdin()

    click.echo()
    click.secho("  DevBrain Setup Wizard", bold=True)
    click.secho("  Local-first persistent memory and dev factory for coding agents", dim=True)
    click.echo()

    try:
        if section is None:
            click.echo("  Every setting can be changed later — pick a section to run,")
            click.echo("  or choose 'Full setup' for the linear first-time flow.")
            _run_menu_loop()
        elif section == "full":
            _run_full_setup()
        else:
            # Find the requested section
            for label, key, runner in MENU_SECTIONS:
                if key == section and runner:
                    click.echo()
                    click.secho(f"  Running: {label}", bold=True)
                    runner()
                    click.echo()
                    print_post_actions()
                    return
            # Unknown section
            valid = [k for _, k, r in MENU_SECTIONS if r or k == "full"]
            _warn(f"Unknown section: '{section}'")
            _info(f"Valid sections: {', '.join(valid)}")
            sys.exit(2)
    except click.exceptions.Abort:
        # User pressed Ctrl+C or stdin EOF. Print a clear recovery message
        # rather than the bare "Aborted!" Click shows by default.
        click.echo()
        click.echo()
        _warn("Setup interrupted.")
        _info("Your progress so far has been saved to config/devbrain.yaml and .env.")
        _info("Re-run 'devbrain setup' anytime to continue — sections are idempotent.")
        sys.exit(1)

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
