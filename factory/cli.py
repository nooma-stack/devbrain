"""DevBrain CLI — dev registration, notification history, telegram setup."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

import click
import yaml

from state_machine import FactoryDB

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"
OLLAMA_URL = "http://localhost:11434"
NL_MODEL = "qwen2.5:7b"


def get_db() -> FactoryDB:
    return FactoryDB(os.environ.get("DEVBRAIN_DATABASE_URL", DATABASE_URL))


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


if __name__ == "__main__":
    cli()
