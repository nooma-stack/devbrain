"""DevBrain CLI — dev registration, notification history, telegram setup."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

import click
import yaml

import schema_migrate
from config import DATABASE_URL, NL_MODEL, OLLAMA_URL
from state_machine import FactoryDB


def get_db() -> FactoryDB:
    return FactoryDB(DATABASE_URL)


def parse_channel(s: str) -> dict:
    """Parse --channel TYPE:ADDRESS into a channel dict."""
    if ":" not in s:
        raise click.BadParameter(f"Channel must be TYPE:ADDRESS, got: {s}")
    ch_type, address = s.split(":", 1)
    return {"type": ch_type.strip(), "address": address.strip()}


@click.group()
def cli():
    """DevBrain CLI — manage devs and notifications."""
    pass


@cli.command()
@click.option("--dev-id", default=None, help="SSH username (defaults to $USER)")
@click.option("--name", default=None, help="Full name")
@click.option(
    "--channel", "channels", multiple=True,
    help="Channel as TYPE:ADDRESS (repeatable). "
         "Types: tmux, smtp, gmail_dwd, gchat_dwd, telegram_bot, "
         "webhook_slack, webhook_discord, webhook_generic",
)
def register(dev_id, name, channels):
    """Register a dev for notifications."""
    dev_id = dev_id or os.environ.get("USER")
    if not dev_id:
        click.echo("Error: --dev-id required (or set $USER)", err=True)
        sys.exit(1)

    parsed_channels = [parse_channel(c) for c in channels]
    db = get_db()
    db.register_dev(dev_id=dev_id, full_name=name, channels=parsed_channels)

    click.echo(f"✅ Dev '{dev_id}' registered with {len(parsed_channels)} channel(s).")
    for c in parsed_channels:
        click.echo(f"   • {c['type']}: {c['address']}")


@cli.command(name="install-identity")
@click.option(
    "--dev-id", default=None,
    help="Dev id to register (defaults to $USER). Skips silently if neither is set.",
)
def install_identity_cmd(dev_id):
    """Non-interactive default dev registration. Called from install.sh."""
    from setup import install_identity as _install_identity
    _install_identity(dev_id=dev_id)


@cli.command(name="add-channel")
@click.option("--dev-id", default=None)
@click.option("--channel", "channel_spec", required=True, help="TYPE:ADDRESS")
def add_channel(dev_id, channel_spec):
    """Add a channel to an existing dev."""
    dev_id = dev_id or os.environ.get("USER")
    db = get_db()
    ch = parse_channel(channel_spec)
    db.add_dev_channel(dev_id, ch)
    click.echo(f"✅ Added {ch['type']}:{ch['address']} to {dev_id}")


@cli.command()
@click.option("--dev", default=None, help="Filter by dev_id (defaults to $USER)")
@click.option("--job", "job_id", default=None, help="Filter by job ID")
@click.option("--event", default=None, help="Filter by event_type")
@click.option("--since", default=None, help="Time window: 1h, 1d, 1w, 1m")
@click.option("--recent", default=None, type=int, help="Show N most recent")
@click.option("--query", "nl_query", default=None, help="Natural language query (via ollama)")
@click.option("--dry-run", is_flag=True, help="For --query: show SQL without executing")
@click.option("--json", "as_json", is_flag=True)
def history(dev, job_id, event, since, recent, nl_query, dry_run, as_json):
    """Browse notification history."""
    db = get_db()

    if nl_query:
        _run_nl_history(db, nl_query, dry_run, as_json)
        return

    since_hours = None
    if since:
        m = re.match(r"(\d+)([hdwm])", since)
        if m:
            num, unit = int(m.group(1)), m.group(2)
            since_hours = num * {"h": 1, "d": 24, "w": 168, "m": 720}[unit]

    if not dev and not job_id and not event and not recent:
        dev = os.environ.get("USER")

    notifs = db.get_notifications(
        recipient_dev_id=dev,
        job_id=job_id,
        event_type=event,
        since_hours=since_hours,
        limit=recent or 50,
    )

    if as_json:
        click.echo(json.dumps(notifs, indent=2, default=str))
        return

    if not notifs:
        click.echo("No notifications found.")
        return

    for n in notifs:
        icon = "✅" if n["channels_delivered"] else "⚠️"
        click.echo(f"\n{icon}  [{n['sent_at'][:19]}] {n['event_type']}")
        click.echo(f"   {n['title']}")
        if n["body"]:
            body = n["body"][:200]
            click.echo(f"   {body}{'...' if len(n['body']) > 200 else ''}")
        if n["channels_delivered"]:
            click.echo(f"   Delivered: {', '.join(n['channels_delivered'])}")
        if n["delivery_errors"]:
            errs = ", ".join(f"{k}: {str(v)[:50]}" for k, v in n["delivery_errors"].items())
            click.echo(f"   Errors: {errs}")


def _run_nl_history(db, query, dry_run, as_json):
    schema = """
CREATE TABLE devbrain.notifications (
    id UUID, recipient_dev_id VARCHAR, job_id UUID,
    event_type VARCHAR, title VARCHAR, body TEXT,
    channels_attempted JSONB, channels_delivered JSONB,
    delivery_errors JSONB, sent_at TIMESTAMPTZ, metadata JSONB
);

CREATE TABLE devbrain.factory_jobs (
    id UUID, title VARCHAR, status VARCHAR, submitted_by VARCHAR, created_at TIMESTAMPTZ
);
"""
    prompt = f"""Convert this natural language query into a single PostgreSQL SELECT.

SCHEMA:
{schema}

QUERY: {query}

RULES:
- Only SELECT, never mutations
- Always LIMIT 50
- Order by sent_at DESC unless specified
- Use 'now() - interval' for time filters
- Prefix tables with devbrain.
- Output ONLY SQL, no explanation, no markdown

SQL:"""

    try:
        data = json.dumps({
            "model": NL_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1},
        }).encode()
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        sql = result["response"].strip()
        sql = re.sub(r"^```sql\s*|\s*```$", "", sql, flags=re.MULTILINE).strip()
    except Exception as e:
        click.echo(f"Error calling ollama at {OLLAMA_URL}: {e}", err=True)
        sys.exit(1)

    if not re.match(r"^\s*SELECT", sql, re.IGNORECASE):
        click.echo(f"Error: generated SQL is not a SELECT:\n{sql}", err=True)
        sys.exit(1)

    if dry_run:
        click.echo(f"Generated SQL:\n{sql}")
        return

    click.echo(f"Running: {sql[:200]}{'...' if len(sql) > 200 else ''}\n")

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
        colnames = [d[0] for d in cur.description] if cur.description else []

    if as_json:
        results = [dict(zip(colnames, r)) for r in rows]
        click.echo(json.dumps(results, indent=2, default=str))
        return

    if not rows:
        click.echo("No results.")
        return

    for row in rows:
        click.echo(str(dict(zip(colnames, row))))


@cli.command()
@click.option("--dev", default=None)
def watch(dev):
    """Tail live notifications (polls every 5s)."""
    dev = dev or os.environ.get("USER")
    db = get_db()
    click.echo(f"Watching notifications for {dev} (Ctrl-C to stop)...\n")
    last_id = None
    try:
        while True:
            notifs = db.get_notifications(recipient_dev_id=dev, limit=5)
            new = []
            for n in notifs:
                if last_id and n["id"] == last_id:
                    break
                new.append(n)
            for n in reversed(new):
                click.echo(f"[{n['sent_at'][:19]}] {n['event_type']}: {n['title']}")
            if notifs:
                last_id = notifs[0]["id"]
            time.sleep(5)
    except KeyboardInterrupt:
        click.echo("\nStopped.")


@cli.command(name="blocked")
@click.option("--project", default=None, help="Filter by project slug")
def blocked(project):
    """List all currently blocked factory jobs."""
    db = get_db()

    with db._conn() as conn, conn.cursor() as cur:
        sql = """
            SELECT j.id, j.title, j.submitted_by, j.blocked_by_job_id,
                   j.updated_at, p.slug
            FROM devbrain.factory_jobs j
            JOIN devbrain.projects p ON j.project_id = p.id
            WHERE j.status = 'blocked'
        """
        params = []
        if project:
            sql += " AND p.slug = %s"
            params.append(project)
        sql += " ORDER BY j.updated_at DESC"
        cur.execute(sql, params)
        rows = cur.fetchall()

    if not rows:
        click.echo("No blocked jobs.")
        return

    for r in rows:
        job_id, title, submitted_by, blocked_by, updated_at, slug = r
        click.echo(f"\n🔒 {title} [{slug}]")
        click.echo(f"   ID: {str(job_id)[:8]}")
        click.echo(f"   Submitted by: {submitted_by or '(unknown)'}")
        click.echo(f"   Blocked by job: {str(blocked_by)[:8] if blocked_by else '(unknown)'}")
        click.echo(f"   Blocked at: {updated_at}")


@cli.command(name="resolve")
@click.argument("job_id")
@click.option("--proceed", "action", flag_value="proceed", help="Use original plan")
@click.option("--replan", "action", flag_value="replan", help="Re-run planning with updated codebase")
@click.option("--cancel", "action", flag_value="cancel", help="Cancel the job")
@click.option("--notes", default=None, help="Optional notes about why")
def resolve(job_id, action, notes):
    """Resolve a blocked job."""
    if not action:
        click.echo("Error: must specify --proceed, --replan, or --cancel", err=True)
        sys.exit(1)

    db = get_db()

    # Resolve short job_id to full UUID
    with db._conn() as conn, conn.cursor() as cur:
        if len(job_id) < 32:
            cur.execute(
                "SELECT id, title FROM devbrain.factory_jobs WHERE id::text LIKE %s AND status = 'blocked' LIMIT 1",
                (f"{job_id}%",),
            )
        else:
            cur.execute(
                "SELECT id, title FROM devbrain.factory_jobs WHERE id = %s",
                (job_id,),
            )
        row = cur.fetchone()

    if not row:
        click.echo(f"No blocked job found matching '{job_id}'.", err=True)
        sys.exit(1)

    full_id, title = row
    full_id = str(full_id)

    # Set the resolution
    db.set_blocked_resolution(full_id, action)

    # Add notes if provided
    if notes:
        import json as _json
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE devbrain.factory_jobs
                   SET metadata = metadata || %s::jsonb
                   WHERE id = %s""",
                (_json.dumps({"resolution_notes": notes}), full_id),
            )
            conn.commit()

    click.echo(f"✅ Resolution '{action}' set for job '{title}' ({full_id[:8]})")

    # Spawn factory process to execute
    import subprocess
    factory_runner = str(Path(__file__).parent / "run.py")
    python_bin = str(Path(__file__).parent.parent / ".venv" / "bin" / "python")
    try:
        subprocess.Popen(
            [python_bin, factory_runner, full_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        click.echo(f"   Factory process spawned to execute resolution.")
    except Exception as e:
        click.echo(f"   ⚠️  Failed to spawn factory: {e}", err=True)
        click.echo(f"   Run manually: {python_bin} {factory_runner} {full_id}")


@cli.command(name="telegram-discover")
@click.option("--dev-id", default=None)
@click.option("--username", default=None, help="Your Telegram username (optional)")
def telegram_discover(dev_id, username):
    """Auto-discover your Telegram chat_id."""
    dev_id = dev_id or os.environ.get("USER")
    if not dev_id:
        click.echo("Error: --dev-id required", err=True)
        sys.exit(1)

    # Load bot token
    config_path = Path(__file__).parent.parent / "config" / "devbrain.yaml"
    bot_token = ""
    bot_username = "your bot"
    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
        tg_config = config.get("notifications", {}).get("channels", {}).get("telegram_bot", {})
        bot_token = tg_config.get("bot_token", "")
        bot_username = tg_config.get("bot_username") or "your bot"
    bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")

    if not bot_token:
        click.echo("Error: Telegram bot token not set", err=True)
        click.echo("Add to config/devbrain.yaml or set TELEGRAM_BOT_TOKEN env var", err=True)
        sys.exit(1)

    click.echo(f"Step 1: On Telegram, DM @{bot_username} with any message (e.g., 'hi').")
    click.pause("Step 2: Press any key here when you've sent the message...")

    from notifications.channels.telegram_bot import TelegramBotChannel
    channel = TelegramBotChannel(bot_token=bot_token)
    chat_id = channel.discover_chat_id(username_hint=username)

    if not chat_id:
        click.echo("❌ Could not find your chat. Make sure you DM'd the bot first.", err=True)
        sys.exit(1)

    # Save to dev's channels
    db = get_db()
    dev = db.get_dev(dev_id)
    if not dev:
        db.register_dev(dev_id=dev_id, channels=[{"type": "telegram_bot", "address": chat_id}])
    else:
        db.add_dev_channel(dev_id, {"type": "telegram_bot", "address": chat_id})

    click.echo(f"✅ Telegram chat_id '{chat_id}' saved for {dev_id}")

    click.echo("Sending test message...")
    result = channel.send(chat_id, "DevBrain Setup Complete", "You're now registered for Telegram notifications.")
    if result.delivered:
        click.echo("✅ Test message delivered.")
    else:
        click.echo(f"⚠️  Test failed: {result.error}")


@cli.command(name="setup")
@click.argument("section", required=False)
def setup_cmd(section):
    """Interactive setup wizard (menu-driven).

    Run with no arguments for the menu. Or jump directly to a section:

      devbrain setup github       — GitHub CLI auth
      devbrain setup ai-clis      — Claude/Codex/Gemini auth (OAuth or API key)
      devbrain setup identity     — register or update your dev identity
      devbrain setup projects     — register projects with DevBrain
      devbrain setup channels     — notification channels (tmux, Slack, Telegram, ...)
      devbrain setup mcp          — auto-configure MCP for installed AI CLIs
      devbrain setup factory-permissions  — set factory CLI permissions tier
      devbrain setup pkrelay      — install optional PKRelay browser bridge
      devbrain setup devdoctor    — run devbrain devdoctor (health check)
      devbrain setup updates      — check for and pull DevBrain updates
      devbrain setup actions      — show remaining post-setup actions
      devbrain setup uninstall    — uninstall DevBrain with dependency choices
      devbrain setup full         — run every section in order (first-time flow)

    `devbrain setup` auto-updates from origin/main before running. Skip
    with DEVBRAIN_NO_UPDATE=1 in your environment.
    """
    from setup import run_setup
    run_setup(section=section)


@cli.command(name="dashboard")
@click.option("--project", default=None, help="Filter by project slug")
def dashboard(project):
    """Launch the DevBrain factory dashboard (TUI)."""
    try:
        from dashboard.app import DashboardApp
    except ImportError as e:
        click.echo(
            f"Error: Textual not installed. Run: pip install textual\n{e}",
            err=True,
        )
        sys.exit(1)

    app = DashboardApp(project=project)
    app.run()


@cli.command(name="status")
@click.option("--project", default=None, help="Filter by project slug")
def status(project):
    """Compact factory status — works great on small screens."""
    from dashboard.data import DashboardData

    db = get_db()
    data = DashboardData(db)

    active = data.get_active_jobs(project=project)
    locks = data.get_active_locks(project=project)
    completed = data.get_recent_completed(project=project, hours=24)

    if not active and not locks and not completed:
        click.echo("All quiet — no active factory jobs.")
        return

    # Active jobs
    if active:
        click.echo(f"\n🟢 Active Jobs ({len(active)})")
        for j in active:
            jid = j["id"][:8]
            status_str = j["status"].upper()[:14]
            title = j["title"][:25]
            dev = f"[{j['submitted_by']}]" if j.get("submitted_by") else ""
            age = _format_age(j.get("updated_at"))
            retry = (
                f" ({j['error_count']}/{j['max_retries']})"
                if j.get("error_count", 0) > 0
                else ""
            )
            click.echo(
                f"  {jid} {status_str:<14} {title:<25} {dev} {age}{retry}"
            )

    # Blocked jobs (subset of active, highlighted separately)
    blocked = [j for j in active if j["status"] == "blocked"] if active else []
    if blocked:
        click.echo(f"\n⚠️  Blocked Jobs ({len(blocked)})")
        for j in blocked:
            jid = j["id"][:8]
            title = j["title"][:30]
            dev = j.get("submitted_by") or "?"
            blocker_id = (
                j.get("blocked_by_job_id", "")[:8]
                if j.get("blocked_by_job_id")
                else "?"
            )
            click.echo(f"  {jid} {title}  [{dev}]")
            click.echo(f"    Blocked by {blocker_id}")
            click.echo(
                f"    Run: devbrain resolve {jid} --proceed|--replan|--cancel"
            )

    # File locks
    if locks:
        click.echo(f"\n🔒 File Locks ({len(locks)})")
        for lk in locks[:10]:  # Cap at 10 for mobile
            path = lk["file_path"]
            if len(path) > 30:
                path = "…" + path[-28:]
            jid = lk["job_id"][:8]
            dev = lk.get("dev_id") or "?"
            click.echo(f"  {path:<30} {jid} ({dev})")
        if len(locks) > 10:
            click.echo(f"  ... and {len(locks) - 10} more")

    # Recent completed
    if completed:
        status_icons = {
            "approved": "✅",
            "deployed": "🚀",
            "rejected": "🚫",
            "failed": "❌",
        }
        click.echo(f"\n📋 Recent Completed ({len(completed)})")
        for j in completed[:8]:  # Cap at 8
            icon = status_icons.get(j["status"], "•")
            jid = j["id"][:8]
            title = j["title"][:30]
            retries = (
                f" ({j['error_count']} retries)"
                if j.get("error_count", 0) > 0
                else ""
            )
            click.echo(f"  {icon} {jid} {j['status']:<9} {title}{retries}")

    if not blocked:
        click.echo("\nNo blocked jobs needing resolution.")
    click.echo()


def _format_age(updated_at) -> str:
    """Format timestamp as human-readable age (5m, 2h, 3d)."""
    if updated_at is None:
        return "?"
    try:
        from datetime import datetime, timezone

        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - updated_at
        seconds = int(delta.total_seconds())
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m"
        if seconds < 86400:
            return f"{seconds // 3600}h"
        return f"{seconds // 86400}d"
    except Exception:
        return "?"


# ─── doctor — installation health check ───────────────────────────────────────


def _peek_container_postgres_password() -> str | None:
    """Return the POSTGRES_PASSWORD env var the devbrain-db container was
    created with, or None if docker isn't available / the container
    doesn't exist / the var isn't present.

    Used by devdoctor to detect a .env/yaml <-> container password
    mismatch — the situation where someone edited config after the
    container was already initialized, so Postgres's stored credentials
    disagree with what the factory and MCP server try to use.
    """
    import subprocess

    try:
        result = subprocess.run(
            ["docker", "inspect", "devbrain-db",
             "--format", "{{range .Config.Env}}{{println .}}{{end}}"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("POSTGRES_PASSWORD="):
            return line.split("=", 1)[1]
    return None


def _diagnose_pg_failure(exc: Exception, cfg: dict) -> str:
    """Produce a helpful detail string for a Postgres connection failure.

    Specifically detect the "yaml/.env password does not match what the
    container was initialized with" scenario by peeking at the
    container's env vars, and steer the user toward the right fix.
    """
    err = str(exc).replace("\n", " ").strip()
    lower = err.lower()

    if "password authentication failed" in lower:
        container_pw = _peek_container_postgres_password()
        if container_pw:
            config_pw = os.environ.get(
                "DEVBRAIN_DB_PASSWORD",
                cfg.get("database", {}).get("password", ""),
            )
            if container_pw != config_pw:
                return (
                    "auth failed — container has a DIFFERENT password "
                    "than .env/yaml. Run: devbrain devdoctor --fix"
                )
        # Auth is failing but the container's POSTGRES_PASSWORD env var
        # (if any) matches config. Most common cause at this point is
        # an ALTER USER on the live container that didn't update config.
        # rotate-db-password can't self-recover (its verify step fails);
        # devdoctor --fix prompts for the live password and syncs.
        return "password authentication failed — run: devbrain devdoctor --fix"

    if "could not connect" in lower or "connection refused" in lower:
        return (
            "Postgres unreachable. Start it: "
            "cd \"$DEVBRAIN_HOME\" && docker compose up -d devbrain-db"
        )

    return err[:160]


def _run_devdoctor_checks() -> list[dict]:
    """Execute every devdoctor health check and return structured results.

    Extracted so `devdoctor`, the legacy `doctor` alias, and the
    `upgrade` command can all share a single source of truth.
    """
    from config import (
        CONFIG_PATH,
        DATABASE_URL,
        DEVBRAIN_HOME,
        load_config,
    )

    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, *, warn: bool = False) -> None:
        status = "WARN" if warn and not ok else ("PASS" if ok else "FAIL")
        checks.append({"name": name, "status": status, "detail": detail})

    # 1. DEVBRAIN_HOME resolves to a real directory
    add(
        "devbrain_home",
        DEVBRAIN_HOME.is_dir(),
        f"{DEVBRAIN_HOME}",
    )

    # 2. Config file present and parses
    cfg: dict = {}
    try:
        cfg = load_config()
        add("config_file", CONFIG_PATH.exists(), f"{CONFIG_PATH}")
    except Exception as exc:
        add("config_file", False, f"parse error: {exc}")

    # 3. Postgres reachable + pgvector extension installed
    try:
        import psycopg2

        conn = psycopg2.connect(DATABASE_URL, connect_timeout=3)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_extension WHERE extname = 'vector';"
            )
            has_vector = cur.fetchone() is not None
        conn.close()
        add("postgres_reachable", True, DATABASE_URL.split("@")[-1])
        add(
            "pgvector_installed",
            has_vector,
            "extension 'vector' present" if has_vector
            else "run: CREATE EXTENSION vector;",
        )
    except Exception as exc:
        add("postgres_reachable", False, _diagnose_pg_failure(exc, cfg))
        add("pgvector_installed", False, "skipped — DB unreachable")

    # 4. Ollama reachable + required models pulled
    embed_model = cfg.get("embedding", {}).get("model", "snowflake-arctic-embed2")
    summary_model = cfg.get("summarization", {}).get("model", "qwen2.5:7b")
    ollama_url = cfg.get("embedding", {}).get("url", "http://localhost:11434")
    try:
        import urllib.error
        import urllib.request

        with urllib.request.urlopen(
            f"{ollama_url.rstrip('/')}/api/tags", timeout=3
        ) as resp:
            tags = json.load(resp)
        models_present = {m.get("name", "").split(":")[0]: m.get("name", "")
                          for m in tags.get("models", [])}
        add("ollama_reachable", True, ollama_url)
        for required in (embed_model, summary_model):
            base = required.split(":")[0]
            present = base in models_present
            add(
                f"ollama_model:{required}",
                present,
                f"have {models_present[base]}" if present
                else f"pull with: ollama pull {required}",
            )
    except Exception as exc:
        add("ollama_reachable", False, f"{ollama_url}: {exc}")
        add(f"ollama_model:{embed_model}", False, "skipped — Ollama unreachable")
        add(f"ollama_model:{summary_model}", False, "skipped — Ollama unreachable")

    # 5. MCP server built
    mcp_dist = DEVBRAIN_HOME / "mcp-server" / "dist" / "index.js"
    add(
        "mcp_server_built",
        mcp_dist.exists(),
        str(mcp_dist) if mcp_dist.exists()
        else "run: cd mcp-server && npm install && npm run build",
    )

    # 6. Ingest venv
    ingest_python = DEVBRAIN_HOME / "ingest" / ".venv" / "bin" / "python"
    add(
        "ingest_venv",
        ingest_python.exists(),
        str(ingest_python) if ingest_python.exists()
        else "run: cd ingest && python3 -m venv .venv && "
             ".venv/bin/pip install -r requirements.txt",
    )

    # 7. Factory permissions tier — tier 3 (unrestricted / legacy default)
    # triggers a WARN since it grants --dangerously-skip-permissions to
    # every spawned factory subprocess. Tiers 1 and 2 are safer.
    factory_cfg = cfg.get("factory", {})
    tier_labels = {1: "read-only audit", 2: "guarded dev", 3: "UNRESTRICTED"}
    if "permissions_tier" in factory_cfg:
        tier = factory_cfg["permissions_tier"]
        tier_is_safe = tier in (1, 2)
        detail = f"tier {tier} ({tier_labels.get(tier, 'unknown')})"
        if tier == 2:
            subs = factory_cfg.get("permissions_tier_2_subcategories", {}) or {}
            enabled = sum(1 for v in subs.values() if v)
            total = len(subs) or 8
            flags = []
            if subs.get("git_push") is False:
                flags.append("git_push=off")
            elif subs.get("git_push") is True:
                flags.append("git_push=on")
            detail += f" — {enabled}/{total} subcategories"
            if flags:
                detail += f" ({', '.join(flags)})"
        elif not tier_is_safe:
            detail += " — run: devbrain setup factory-permissions"
        add("factory_permissions_tier", tier_is_safe, detail, warn=True)
    else:
        add(
            "factory_permissions_tier",
            False,
            "not set — defaulting to tier 3 (unrestricted). "
            "Run: devbrain setup factory-permissions",
            warn=True,
        )

    # 8. DB password isn't the insecure default from earlier templates.
    # `devbrain-local` was shipped in git history of a public repo; any
    # install still using it has a trivially-known password. Warn (not
    # fail) since the system is functional — user just needs to rotate.
    weak_passwords = {"devbrain-local", "REPLACE_DURING_INSTALL", ""}
    effective_pw = os.environ.get(
        "DEVBRAIN_DB_PASSWORD",
        cfg.get("database", {}).get("password", ""),
    )
    pw_is_strong = effective_pw not in weak_passwords
    add(
        "db_password_rotated",
        pw_is_strong,
        "custom password in use" if pw_is_strong
        else "weak/default password — run: devbrain rotate-db-password",
        warn=True,
    )

    # 9. AI CLI login status. A missing login is invisible until a factory
    # subprocess fires off with `claude -p ...` and exits with "Not logged
    # in · Please run /login", which only surfaces deep inside the
    # orchestrator log. Surface it here so install-time or pre-flight
    # devdoctor catches it.
    import shutil as _shutil
    import subprocess as _subprocess
    for _ai_cli in ("claude", "gemini"):
        if not _shutil.which(_ai_cli):
            continue
        login_flag = "-p"
        try:
            _res = _subprocess.run(
                [_ai_cli, login_flag, "ping"],
                capture_output=True, text=True, timeout=15,
            )
            _blob = (_res.stdout + "\n" + _res.stderr).lower()
            _authed = not any(
                s in _blob
                for s in ("not logged in", "please run /login",
                          "please log in", "auth required")
            )
            add(
                f"ai_cli_logged_in:{_ai_cli}",
                _authed,
                "logged in" if _authed
                else f"not logged in — run: {_ai_cli} /login",
                warn=True,
            )
        except (_subprocess.TimeoutExpired, FileNotFoundError):
            # Timeout usually means the CLI is in some interactive
            # state — treat as non-fatal.
            add(f"ai_cli_logged_in:{_ai_cli}", True,
                "probe timed out (treating as authed)", warn=True)

    # 10. Env vars (informational — never fails, just reports overrides)
    overrides = sorted(k for k in os.environ if k.startswith("DEVBRAIN_"))
    add(
        "env_overrides",
        True,
        ", ".join(overrides) if overrides else "(none — using yaml + defaults)",
    )

    return checks


def _render_devdoctor_report(checks: list[dict]) -> None:
    """Print the human-readable devdoctor report."""
    click.echo("DevDoctor")
    click.echo("=" * 60)
    for c in checks:
        icon = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌"}[c["status"]]
        click.echo(f"  {icon} {c['name']:<32} {c['detail']}")
    click.echo()


def _offer_devdoctor_fixes(checks: list[dict]) -> None:
    """Interactively offer to remediate WARN/FAIL items found by devdoctor.

    Each remediation is opt-in with y/N prompts. Actions that affect
    long-running processes (notably the Postgres container) also print
    an explicit "restart your Claude Code sessions afterwards" reminder,
    because the MCP subprocess reads yaml once at startup and won't
    notice a rotated password until it re-launches.
    """
    actionable = [c for c in checks if c["status"] in ("WARN", "FAIL")]
    if not actionable:
        click.echo("✅ Nothing to fix.")
        return

    ctx = click.get_current_context()

    click.echo()
    click.secho("── Interactive remediation ─────────────────────────────────",
                bold=True)
    click.echo()
    click.echo("For each flagged item, confirm y/N to apply the fix.")
    click.echo("After any fix that recreates the database container, open a")
    click.echo("new terminal (and restart any Claude Code session using")
    click.echo("DevBrain MCP) so the MCP subprocess reloads.")
    click.echo()

    for c in actionable:
        name = c["name"]
        detail = c["detail"]
        icon = {"WARN": "⚠️ ", "FAIL": "❌"}[c["status"]]
        click.secho(f"{icon} {name}", bold=True)
        click.echo(f"   {detail}")

        if name == "db_password_rotated":
            click.echo("   Fix: generate a new password, ALTER USER inside the")
            click.echo("        container, sync .env + yaml, recreate the container.")
            if click.confirm("   Rotate DB password now?", default=True):
                ctx.invoke(rotate_db_password, yes=False, recreate=True)
                click.secho(
                    "   → After this runs, open a new terminal (and restart "
                    "any Claude Code sessions) before using DevBrain MCP tools.",
                    fg="yellow",
                )

        elif name == "factory_permissions_tier":
            click.echo("   Fix: interactive wizard to pick tier + subcategories.")
            if click.confirm("   Run factory-permissions wizard now?", default=True):
                from setup import run_setup
                run_setup(section="factory-permissions")

        elif name == "mcp_server_built":
            click.echo("   Fix: rebuild the MCP server (npm install + build).")
            if click.confirm("   Rebuild now?", default=True):
                import subprocess
                from config import DEVBRAIN_HOME
                mcp_dir = DEVBRAIN_HOME / "mcp-server"
                subprocess.call(["npm", "install", "--silent"], cwd=str(mcp_dir))
                subprocess.call(["npm", "run", "build", "--silent"], cwd=str(mcp_dir))
                click.secho(
                    "   → Restart any running Claude Code sessions so the MCP"
                    " subprocess picks up the rebuilt dist/.",
                    fg="yellow",
                )

        elif name == "postgres_reachable":
            import psycopg2

            from config import CONFIG_PATH, DEVBRAIN_HOME, load_config

            container_pw = _peek_container_postgres_password()
            cfg_now = load_config()
            db_cfg = cfg_now.get("database", {})
            effective_pw = os.environ.get(
                "DEVBRAIN_DB_PASSWORD",
                db_cfg.get("password", ""),
            )

            recovered_pw: str | None = None

            if container_pw and container_pw != effective_pw:
                # Case A — POSTGRES_PASSWORD env on the container differs
                # from .env/yaml. Classic "config was edited after init"
                # drift. We can auto-recover by copying the container
                # value into config (container's stored auth still
                # matches its original env — Postgres was initialized
                # from POSTGRES_PASSWORD and no one's touched it since).
                click.echo(
                    "   Detected: devbrain-db container was initialized with"
                )
                click.echo(
                    "             a different POSTGRES_PASSWORD than your .env/yaml."
                )
                if click.confirm(
                    "   Sync .env + yaml to match the container?", default=True
                ):
                    recovered_pw = container_pw
            else:
                # Case B — env var matches config but auth still fails.
                # Someone ran ALTER USER on the live container (either
                # manually or via a partial rotation). POSTGRES_PASSWORD
                # env is stale; we can't introspect the live credential
                # from outside. Ask the user, verify by opening a
                # connection, then use that as the recovered password.
                click.echo(
                    "   The container's POSTGRES_PASSWORD env var matches"
                )
                click.echo(
                    "   your config, but auth is still failing. Likely"
                )
                click.echo(
                    "   an ALTER USER ran on the live container and the"
                )
                click.echo(
                    "   env var is stale. Enter the current live password"
                )
                click.echo(
                    "   and we'll verify + sync + optionally rotate forward."
                )
                if click.confirm(
                    "   Enter the current DB password now?", default=True
                ):
                    manual_pw = click.prompt(
                        "   Current password",
                        hide_input=True,
                        confirmation_prompt=False,
                        default="",
                        show_default=False,
                    ).strip()
                    if not manual_pw:
                        click.echo("   (no password entered — skipping)")
                    else:
                        test_url = (
                            f"postgresql://"
                            f"{db_cfg.get('user', 'devbrain')}:{manual_pw}"
                            f"@{db_cfg.get('host', 'localhost')}:"
                            f"{db_cfg.get('port', 5433)}"
                            f"/{db_cfg.get('database', 'devbrain')}"
                        )
                        try:
                            psycopg2.connect(
                                test_url, connect_timeout=5
                            ).close()
                            click.echo("   ✓ password verified")
                            recovered_pw = manual_pw
                        except psycopg2.Error as exc:
                            click.echo(
                                f"   ✗ that password didn't work: "
                                f"{str(exc).splitlines()[0]}",
                                err=True,
                            )

            if recovered_pw:
                env_path = DEVBRAIN_HOME / ".env"
                _rewrite_env_password(env_path, recovered_pw)
                _rewrite_yaml_db_password(CONFIG_PATH, recovered_pw)
                click.echo("   ✓ .env + yaml now match the DB")
                if click.confirm(
                    "   Also rotate to a fresh password and recreate the "
                    "container (applies loopback binding)?",
                    default=True,
                ):
                    ctx.invoke(rotate_db_password, yes=False, recreate=True)
                    click.secho(
                        "   → Restart any Claude Code sessions using "
                        "DevBrain MCP so their subprocesses reload.",
                        fg="yellow",
                    )
                else:
                    click.secho(
                        "   → The container is still on its old port "
                        "binding (0.0.0.0). Run rotate-db-password later "
                        "to tighten to 127.0.0.1.",
                        fg="yellow",
                    )
            else:
                click.echo(
                    "   Skipped — no recovery action taken. If you know"
                )
                click.echo(
                    "   the live DB password, you can also pass it to"
                )
                click.echo(
                    f"   {click.style('devbrain rotate-db-password --current-password ...', fg='cyan')}"
                )

        elif name.startswith("ollama_model:"):
            model = name.split(":", 1)[1]
            click.echo(f"   Fix: pull {model} via Ollama.")
            if click.confirm("   Pull now?", default=True):
                import subprocess
                subprocess.call(["ollama", "pull", model])

        elif name == "pgvector_installed":
            click.echo("   Fix: run CREATE EXTENSION vector; in the devbrain DB.")
            if click.confirm("   Create the extension now?", default=True):
                try:
                    import psycopg2
                    from config import DATABASE_URL
                    with psycopg2.connect(DATABASE_URL) as conn, conn.cursor() as cur:
                        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
                    click.echo("   ✓ vector extension present")
                except Exception as exc:
                    click.echo(f"   ✗ {exc}", err=True)

        else:
            click.echo("   (no automated remediation — see INSTALL.md)")

        click.echo()

    click.echo("Re-run 'devbrain devdoctor' to verify.")


@cli.command(name="devdoctor")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text")
@click.option("--fix", is_flag=True,
              help="Interactively remediate WARN/FAIL items")
def devdoctor(as_json: bool, fix: bool) -> None:
    """Verify DevBrain installation. Exit 0 only if every check passes.

    Checks: Postgres + pgvector, Ollama + required models, MCP server build,
    ingest venv, config file validity, factory permissions tier, DB
    password strength, and reports any env var overrides.

    With --fix, devdoctor walks each WARN/FAIL item and offers to
    remediate (rotate DB password, set factory tier, rebuild MCP, etc.).
    """
    checks = _run_devdoctor_checks()

    if as_json:
        click.echo(json.dumps(checks, indent=2))
    else:
        _render_devdoctor_report(checks)

    failed = [c for c in checks if c["status"] == "FAIL"]
    warned = [c for c in checks if c["status"] == "WARN"]

    if fix and not as_json and (failed or warned):
        _offer_devdoctor_fixes(checks)
        # Don't sys.exit after a fix pass — user gets to see results
        # and re-run devdoctor manually.
        return

    if failed:
        if not as_json:
            click.echo(
                f"❌ {len(failed)} check(s) failed. See INSTALL.md for setup steps.",
                err=True,
            )
            if warned:
                click.echo(
                    f"   {len(warned)} warning(s) — run 'devbrain devdoctor --fix' "
                    "to remediate interactively.",
                    err=True,
                )
        sys.exit(1)

    if not as_json:
        if warned:
            click.echo(
                f"⚠️  {len(warned)} warning(s) — run 'devbrain devdoctor --fix' "
                "to remediate interactively."
            )
        else:
            click.echo("✅ All checks passed.")


@cli.command(name="version")
def version() -> None:
    """Print DevBrain version info: git commit, branch, working tree, DEVBRAIN_HOME."""
    import subprocess
    from config import DEVBRAIN_HOME

    def _git(args: list[str]) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(DEVBRAIN_HOME),
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    commit = _git(["rev-parse", "--short", "HEAD"])
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    porcelain = _git(["status", "--porcelain"])

    if commit is None:
        commit_str = "git not available"
        branch_str = "git not available"
        tree_str = "git not available"
    else:
        commit_str = commit
        # `--abbrev-ref HEAD` returns the literal "HEAD" in detached state
        # (e.g. CI checkouts by SHA). Surface that as "(detached)".
        branch_str = "(detached)" if branch in (None, "HEAD") else branch
        tree_str = "clean" if porcelain == "" else "dirty"

    click.echo(f"commit: {commit_str}")
    click.echo(f"branch: {branch_str}")
    click.echo(f"working tree: {tree_str}")
    click.echo(f"DEVBRAIN_HOME: {DEVBRAIN_HOME}")


@cli.command(name="doctor", hidden=True)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text")
@click.option("--fix", is_flag=True, help="Interactively remediate WARN/FAIL items")
@click.pass_context
def doctor_alias(ctx: click.Context, as_json: bool, fix: bool) -> None:
    """Legacy alias for `devdoctor`. Kept so existing scripts keep working."""
    click.echo(
        "(Note: 'doctor' has been renamed to 'devdoctor'. "
        "The old name still works; prefer the new one in scripts.)",
        err=True,
    )
    ctx.invoke(devdoctor, as_json=as_json, fix=fix)


def _rewrite_env_password(env_path: Path, new_password: str) -> None:
    """Replace (or append) DEVBRAIN_DB_PASSWORD in a .env file."""
    if env_path.exists():
        lines = [
            ln for ln in env_path.read_text().splitlines()
            if not ln.startswith("DEVBRAIN_DB_PASSWORD=")
        ]
    else:
        lines = []
    lines.append("")
    lines.append(f"# Database password — rotated {time.strftime('%Y-%m-%d')}")
    lines.append(f"DEVBRAIN_DB_PASSWORD={new_password}")
    env_path.write_text("\n".join(lines) + "\n")


def _rewrite_yaml_db_password(yaml_path: Path, new_password: str) -> None:
    """Replace password: under database: in config/devbrain.yaml.

    Line-based rewrite instead of round-tripping through PyYAML (which
    would strip comments and re-order keys). Scope is limited to the
    database: block to avoid touching notification-channel passwords.
    """
    out: list[str] = []
    in_db_block = False
    replaced = False
    for line in yaml_path.read_text().splitlines():
        if re.match(r"^database:", line):
            in_db_block = True
            out.append(line)
            continue
        if in_db_block and re.match(r"^  password:", line):
            out.append(f"  password: {new_password}")
            replaced = True
            continue
        if re.match(r"^[^\s#]", line):
            in_db_block = False
        out.append(line)
    if not replaced:
        raise click.ClickException(
            f"Could not find 'password:' under 'database:' in {yaml_path}"
        )
    yaml_path.write_text("\n".join(out) + "\n")


@cli.command(name="rotate-db-password")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option(
    "--recreate/--no-recreate",
    default=True,
    help="Recreate the container after rotation to apply any "
         "docker-compose.yml changes (default: yes)",
)
@click.option(
    "--current-password",
    default=None,
    help="Use this value as the CURRENT DB password (instead of reading "
         "from .env/yaml). Useful after an ALTER USER that left config "
         "and the live DB out of sync — pass the live password here and "
         "rotation will sync config to match on success.",
)
def rotate_db_password(
    yes: bool, recreate: bool, current_password: str | None,
) -> None:
    """Rotate the Postgres password, preserving data.

    Generates a new random password, applies it inside the running
    container via ALTER USER, then syncs the new value to .env and
    config/devbrain.yaml. Optionally recreates the container so updated
    docker-compose settings (e.g., loopback-only port binding) take effect.

    Use --current-password when .env/yaml drifted from the live DB
    (typically after a manual ALTER USER). Rotation will authenticate
    with the supplied value and write the new password to both files.
    """
    import secrets
    import subprocess

    import psycopg2
    from psycopg2 import sql

    from config import CONFIG_PATH, DEVBRAIN_HOME, build_database_url, load_config

    # Refuse to run if DEVBRAIN_DATABASE_URL overrides the config — the
    # user wired credentials up explicitly and this command wouldn't help.
    if os.environ.get("DEVBRAIN_DATABASE_URL"):
        raise click.ClickException(
            "DEVBRAIN_DATABASE_URL is set in the environment, which overrides "
            "config/devbrain.yaml. Unset it (or rotate manually) before using "
            "this command."
        )

    cfg = load_config()
    db_cfg = cfg.get("database", {})
    db_user = db_cfg.get("user", "devbrain")
    db_name = db_cfg.get("database", "devbrain")
    db_host = db_cfg.get("host", "localhost")
    db_port = db_cfg.get("port", 5433)
    if current_password is not None:
        # User-supplied recovery password — build a URL with it instead
        # of trusting config, which may be out of sync with the live DB.
        current_url = (
            f"postgresql://{db_user}:{current_password}"
            f"@{db_host}:{db_port}/{db_name}"
        )
    else:
        current_url = build_database_url(cfg)
    env_path = DEVBRAIN_HOME / ".env"
    yaml_path = CONFIG_PATH

    click.echo("DevBrain — rotate database password")
    click.echo("=" * 60)
    click.echo(f"  User:     {db_user}")
    click.echo(f"  Host:     {db_host}:{db_port}")
    click.echo(f"  Database: {db_name}")
    click.echo(f"  .env:     {env_path}")
    click.echo(f"  yaml:     {yaml_path}")
    if current_password is not None:
        click.echo("  Source:   --current-password flag (recovery mode)")
    click.echo()

    if not yes and not click.confirm("Rotate the password now?", default=True):
        click.echo("Aborted.")
        return

    # Step 1: verify the current password actually works. If it doesn't,
    # there's no point generating a new one — we couldn't apply it.
    click.echo("→ Connecting with current password...", nl=False)
    try:
        verify_conn = psycopg2.connect(current_url, connect_timeout=5)
    except psycopg2.Error as exc:
        click.echo(" ❌")
        hint = (
            "If the config drifted from the live DB (e.g. someone ran "
            "ALTER USER manually), retry with:\n"
            "    devbrain rotate-db-password --current-password '<live-pw>'\n"
            "Or run 'devbrain devdoctor --fix' to interactively recover."
        )
        raise click.ClickException(
            f"Can't connect with the current password: {exc}\n{hint}"
        )
    click.echo(" ✅")

    # Step 2: generate and apply new password. ALTER USER takes effect
    # immediately for new connections; existing ones keep working until
    # they disconnect.
    new_password = secrets.token_hex(32)
    click.echo("→ Applying new password via ALTER USER...", nl=False)
    try:
        with verify_conn:
            with verify_conn.cursor() as cur:
                cur.execute(
                    sql.SQL("ALTER USER {user} PASSWORD {pw}").format(
                        user=sql.Identifier(db_user),
                        pw=sql.Literal(new_password),
                    )
                )
    except psycopg2.Error as exc:
        click.echo(" ❌")
        verify_conn.close()
        raise click.ClickException(f"ALTER USER failed: {exc}")
    verify_conn.close()
    click.echo(" ✅")

    # Step 3: verify we can connect with the NEW password before writing
    # it to .env/yaml. If this fails, the DB has a password we can't
    # recover from config, so print it loudly so the user can paste it in.
    click.echo("→ Verifying new password...", nl=False)
    new_url = (
        f"postgresql://{db_user}:{new_password}"
        f"@{db_host}:{db_port}/{db_name}"
    )
    try:
        psycopg2.connect(new_url, connect_timeout=5).close()
    except psycopg2.Error as exc:
        click.echo(" ❌")
        click.echo(
            f"\nALTER USER succeeded but the new password doesn't connect: {exc}",
            err=True,
        )
        click.echo(
            "\nThe database now has this password (NOT yet written to disk):",
            err=True,
        )
        click.echo(f"\n  {new_password}\n", err=True)
        click.echo(
            "Update .env (DEVBRAIN_DB_PASSWORD) and config/devbrain.yaml "
            "(database.password) manually, then re-run 'devbrain doctor'.",
            err=True,
        )
        sys.exit(1)
    click.echo(" ✅")

    # Step 4: write to .env and yaml. Order: .env first (smaller blast
    # radius if the process dies between them — re-running this command
    # will pick up .env and sync yaml).
    click.echo("→ Writing DEVBRAIN_DB_PASSWORD to .env...", nl=False)
    _rewrite_env_password(env_path, new_password)
    click.echo(" ✅")

    click.echo("→ Updating config/devbrain.yaml...", nl=False)
    _rewrite_yaml_db_password(yaml_path, new_password)
    click.echo(" ✅")

    # Step 5: optionally recreate the container so docker-compose.yml
    # changes (port binding, etc.) take effect. Password rotation alone
    # doesn't require a recreate — ALTER USER already applied it.
    if recreate:
        if subprocess.run(
            ["docker", "--version"], capture_output=True
        ).returncode != 0:
            click.echo(
                "⚠️  docker not available — skipping container recreate.",
                err=True,
            )
        else:
            click.echo("→ Recreating devbrain-db container...")
            compose_dir = str(DEVBRAIN_HOME)
            down_rc = subprocess.call(
                ["docker", "compose", "down"], cwd=compose_dir
            )
            if down_rc != 0:
                click.echo(
                    "⚠️  'docker compose down' returned non-zero. Continuing.",
                    err=True,
                )
            up_rc = subprocess.call(
                ["docker", "compose", "up", "-d", "devbrain-db"],
                cwd=compose_dir,
            )
            if up_rc != 0:
                raise click.ClickException(
                    "'docker compose up -d devbrain-db' failed. The new "
                    "password is already in .env/yaml and stored in Postgres "
                    "— fix docker-compose errors and bring the container up "
                    "manually."
                )

            # Poll until Postgres accepts connections again
            click.echo("→ Waiting for Postgres to accept connections...", nl=False)
            for _ in range(30):
                try:
                    psycopg2.connect(new_url, connect_timeout=1).close()
                    click.echo(" ✅")
                    break
                except psycopg2.Error:
                    time.sleep(1)
            else:
                click.echo(" ⚠️")
                click.echo(
                    "Container is up but Postgres didn't accept connections "
                    "within 30s. Check 'docker logs devbrain-db'.",
                    err=True,
                )

    click.echo()
    click.echo("✅ Rotation complete.")
    click.echo()
    click.echo("Next: run './bin/devbrain devdoctor' to verify.")


@cli.command(name="upgrade")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
@click.option("--no-pull", is_flag=True, help="Skip git pull")
@click.option("--no-rebuild", is_flag=True, help="Skip MCP rebuild")
@click.option("--no-rotate", is_flag=True, help="Skip DB rotation check")
@click.option("--no-tier", is_flag=True, help="Skip factory tier check")
@click.pass_context
def upgrade(
    ctx: click.Context,
    yes: bool,
    no_pull: bool,
    no_rebuild: bool,
    no_rotate: bool,
    no_tier: bool,
) -> None:
    """Migrate an existing install to the latest defaults.

    Chains five steps:

    \b
      1. git pull --ff-only           (skip with --no-pull)
      2. Rebuild the MCP server       (skip with --no-rebuild)
      3. Rotate DB password if weak   (skip with --no-rotate)
      4. Set factory tier if unsafe   (skip with --no-tier)
      5. Run devdoctor for verification

    Intended for existing DevBrain installs that pre-date newer defaults
    (random DB password, loopback-only Postgres binding, factory
    permission tiers). Idempotent — steps whose condition is already
    satisfied skip with a checkmark.

    Run this from a regular terminal (not inside a Claude Code session
    whose MCP subprocess is connected to the DB). After completion,
    restart any Claude Code sessions so the rebuilt MCP + rotated
    password take effect.
    """
    import subprocess

    from config import DEVBRAIN_HOME, load_config

    click.echo()
    click.secho("DevBrain Upgrade", bold=True)
    click.echo("=" * 66)
    click.echo()
    click.echo("Steps:")
    click.echo("  1. git pull --ff-only")
    click.echo("  2. Rebuild MCP server (npm install + build)")
    click.echo("  3. Rotate DB password if still on the old devbrain-local default")
    click.echo("  4. Prompt for factory permissions tier if set to 3 / unset")
    click.echo("  5. Run devdoctor")
    click.echo()
    click.secho(
        "⚠️  Restart any running Claude Code sessions after this finishes —",
        fg="yellow",
    )
    click.secho(
        "   their MCP subprocesses keep the old dist/ and yaml password in memory.",
        fg="yellow",
    )
    click.echo()

    if not yes and not click.confirm("Proceed?", default=True):
        click.echo("Aborted.")
        return

    # ─── Step 1: git pull ────────────────────────────────────────────────
    click.echo()
    click.secho("[1/5] git pull --ff-only", bold=True)
    if no_pull:
        click.echo("   (skipped via --no-pull)")
    else:
        rc = subprocess.call(
            ["git", "pull", "--ff-only"], cwd=str(DEVBRAIN_HOME)
        )
        if rc != 0:
            raise click.ClickException(
                "git pull failed — resolve manually, then re-run 'devbrain upgrade'."
            )

    # ─── Step 2: rebuild MCP ─────────────────────────────────────────────
    click.echo()
    click.secho("[2/5] Rebuild MCP server", bold=True)
    if no_rebuild:
        click.echo("   (skipped via --no-rebuild)")
    else:
        mcp_dir = DEVBRAIN_HOME / "mcp-server"
        if not mcp_dir.is_dir():
            click.echo("   (mcp-server/ not present — skipped)")
        else:
            click.echo("   npm install...")
            rc = subprocess.call(
                ["npm", "install", "--silent"], cwd=str(mcp_dir)
            )
            if rc == 0:
                click.echo("   npm run build...")
                rc = subprocess.call(
                    ["npm", "run", "build", "--silent"], cwd=str(mcp_dir)
                )
            if rc != 0:
                raise click.ClickException("MCP rebuild failed")
            click.echo("   ✓ rebuilt")

    # ─── Step 3: DB password ────────────────────────────────────────────
    click.echo()
    click.secho("[3/5] Check DB password", bold=True)
    if no_rotate:
        click.echo("   (skipped via --no-rotate)")
    else:
        # Re-import config after git pull in case defaults changed.
        cfg = load_config()
        weak = {"devbrain-local", "REPLACE_DURING_INSTALL", ""}
        pw = os.environ.get(
            "DEVBRAIN_DB_PASSWORD",
            cfg.get("database", {}).get("password", ""),
        )
        if pw in weak:
            click.echo(
                "   Weak/default password detected — rotating now."
            )
            click.echo(
                "   (This recreates the devbrain-db container with the"
                " loopback-only port binding.)"
            )
            ctx.invoke(rotate_db_password, yes=yes, recreate=True)
        else:
            click.echo("   ✓ custom password in use")

    # ─── Step 4: factory tier ───────────────────────────────────────────
    click.echo()
    click.secho("[4/5] Check factory permissions tier", bold=True)
    if no_tier:
        click.echo("   (skipped via --no-tier)")
    else:
        cfg = load_config()
        factory_cfg = cfg.get("factory", {})
        tier = factory_cfg.get("permissions_tier")
        if tier in (1, 2):
            click.echo(f"   ✓ tier {tier}")
        else:
            click.echo(
                f"   Tier {tier!r} (unrestricted or unset) — launching "
                "factory-permissions wizard."
            )
            from setup import run_setup
            run_setup(section="factory-permissions")

    # ─── Step 5: devdoctor ──────────────────────────────────────────────
    click.echo()
    click.secho("[5/5] Final health check", bold=True)
    try:
        ctx.invoke(devdoctor, as_json=False, fix=False)
    except SystemExit as exc:
        # devdoctor calls sys.exit(1) on FAIL. Don't let that abort
        # the friendly post-message we owe the user.
        if exc.code not in (None, 0):
            click.echo()
            click.secho(
                "⚠️  devdoctor reported failures — review above and run "
                "'devbrain devdoctor --fix' to remediate.",
                fg="yellow",
            )

    click.echo()
    click.secho("✅ Upgrade complete.", fg="green", bold=True)
    click.echo()
    click.secho(
        "Next: restart any Claude Code sessions that use DevBrain MCP",
        fg="yellow",
    )
    click.secho(
        "      (their MCP subprocesses still hold the pre-upgrade state).",
        fg="yellow",
    )


@cli.command(name="migrate")
@click.option("--dry-run", is_flag=True, help="List pending migrations without applying.")
@click.option(
    "--migrations-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Override the migrations directory (defaults to $DEVBRAIN_HOME/migrations).",
)
def migrate(dry_run: bool, migrations_dir: Path | None) -> None:
    """Apply pending DB schema migrations.

    Idempotent: only files not yet recorded in devbrain.schema_migrations
    are run, and concurrent invocations coordinate via a Postgres
    advisory lock.
    """
    # Surface the per-file [migrate] applied X.sql (Yms) lines from
    # schema_migrate.logger to stdout. basicConfig is a no-op if logging
    # is already configured (e.g. inside a test harness), so this is
    # safe to call at command entry.
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    db = get_db()
    try:
        result = schema_migrate.migrate(
            db, migrations_dir=migrations_dir, dry_run=dry_run,
        )
    except Exception as exc:
        click.echo(f"[migrate] FAILED: {exc}", err=True)
        sys.exit(1)

    if dry_run:
        if result:
            click.echo("[migrate] pending migrations:")
            for name in result:
                click.echo(f"  {name}")
        else:
            click.echo("[migrate] no pending migrations")
    elif not result:
        click.echo("[migrate] no pending migrations")


if __name__ == "__main__":
    cli()
