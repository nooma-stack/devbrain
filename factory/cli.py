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

import attribute_orphans
import backfill_memory
import export_memory
import import_memory
import schema_migrate
from config import DATABASE_URL, NL_MODEL, OLLAMA_URL
from cred_rotate import (
    rewrite_env_password as _rewrite_env_password,
    rewrite_yaml_db_password as _rewrite_yaml_db_password,
)
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


# Late-imported registration hook for project + port-registry commands.
# Done at import time so they appear in --help. Imports inside the function
# are deferred until first invocation (avoids loading psycopg2 / DB layer
# on `--help` paths that don't need it).
def _register_project_cli() -> None:
    try:
        import project_cli
        project_cli.register(cli)
    except ImportError as e:
        logger.debug("project_cli not available: %s", e)


_register_project_cli()


def _register_audit_cli() -> None:
    try:
        import audit_cli
        audit_cli.register(cli)
    except ImportError as e:
        logger.debug("audit_cli not available: %s", e)


_register_audit_cli()


def _resolve_cli_names(cli_arg: str) -> list[str]:
    """Resolve `--cli` option into the list of adapter names."""
    import dev_login as _dl
    if cli_arg == "all":
        return _dl.default_registry.list_names()
    return [cli_arg]


def _validate_cli_choice(ctx, param, value):
    """Click option callback — validate --cli against the live registry."""
    if value is None:
        return value
    import dev_login as _dl
    valid = list(_dl.default_registry.list_names()) + ["all"]
    if value not in valid:
        raise click.BadParameter(f"must be one of {valid}, got {value!r}")
    return value


@cli.command()
@click.option("--dev", "dev_id", required=True, help="Dev id to log in (lowercase short name)")
@click.option(
    "--cli", "cli_arg",
    callback=_validate_cli_choice,
    default="all",
    show_default=True,
    help="Which AI CLI to log in (claude, codex, gemini, or 'all'). 'all' runs each in turn.",
)
@click.option("--git-name", default=None, help="Git author name (skips prompt if --git-email also set)")
@click.option("--git-email", default=None, help="Git author email")
@click.option(
    "--keychain-password", default=None,
    help="Custom password for the per-profile macOS keychain (claude-only). "
         "Stored at <profile>/.claude/.keychain-password (mode 600), read by the "
         "factory orchestrator before each spawn. If omitted on first claude "
         "login: prompts to choose Random (recommended) or Custom interactively, "
         "or generates a random password non-interactively.",
)
@click.option(
    "--oauth-token", "oauth_token", default=None,
    help="Long-lived Claude Code OAuth token (sk-ant-oat01-...) generated "
         "by `claude setup-token` on the dev's laptop. When set, the factory "
         "spawns claude with CLAUDE_CODE_OAUTH_TOKEN=<token> instead of "
         "relying on the per-profile macOS keychain — fully SSH/headless-"
         "friendly, preserves the dev's Pro/Max/Team subscription billing. "
         "Stashed at <profile>/.claude/oauth-token (mode 600). The dev should "
         "treat this string as a credential.",
)
def login(dev_id, cli_arg, git_name, git_email, keychain_password, oauth_token):
    """Log a dev into an AI CLI's per-dev profile."""
    import dev_login

    db = get_db()

    cli_names_resolved = _resolve_cli_names(cli_arg)

    # Stash the OAuth token (claude only). Once stashed, the factory's
    # cli_executor will inject it as CLAUDE_CODE_OAUTH_TOKEN env var on
    # every claude spawn for this dev — no Keychain involvement. We
    # intentionally do this BEFORE running login_dev so a token-only
    # onboarding (no Keychain provisioning) skips the OAuth subprocess.
    if "claude" in cli_names_resolved and oauth_token:
        if not oauth_token.startswith("sk-ant-oat01-") and not oauth_token.startswith("sk-ant-"):
            click.echo(
                "Error: --oauth-token doesn't look like a Claude Code OAuth "
                "token (expected sk-ant-oat01-...). Get one with `claude "
                "setup-token` on a machine with a browser.",
                err=True,
            )
            sys.exit(1)
        from profiles import get_profile_dir, validate_dev_id
        validate_dev_id(dev_id)
        profile_dir = get_profile_dir(dev_id)
        token_file = profile_dir / ".claude" / "oauth-token"
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(oauth_token.strip())
        token_file.chmod(0o600)
        click.echo(f"✅ {dev_id} → claude  (oauth-token stashed at {token_file})")
        # If --cli was 'claude' (the only CLI), we're done — no need to
        # invoke the interactive Keychain login flow at all. Drop claude
        # from the list so login_dev doesn't process it.
        cli_names_resolved = [c for c in cli_names_resolved if c != "claude"]
        if not cli_names_resolved:
            return

    # Stash a custom keychain password BEFORE login_dev runs the claude
    # adapter, so claude.py:_read_or_generate_keychain_password picks it
    # up. If --keychain-password wasn't passed and we're interactive AND
    # this is a fresh keychain provisioning (no password file yet), prompt
    # the dev to choose between Random and Custom. Random + non-interactive
    # both fall through to claude.py auto-generating a secure default.
    if "claude" in cli_names_resolved:
        from profiles import get_profile_dir
        profile_dir = get_profile_dir(dev_id)
        pw_file = profile_dir / ".claude" / ".keychain-password"
        if not pw_file.exists():
            if keychain_password is None and sys.stdin.isatty():
                click.echo(
                    "First-time claude login provisions a per-dev macOS keychain "
                    "to isolate this dev's OAuth tokens from lhtdev's main keychain.",
                )
                choice = click.prompt(
                    "Keychain password: [r]andom (recommended) or [c]ustom",
                    type=click.Choice(["r", "c", "R", "C"], case_sensitive=False),
                    default="r",
                    show_choices=False,
                    show_default=True,
                ).lower()
                if choice == "c":
                    keychain_password = click.prompt(
                        "Enter keychain password",
                        hide_input=True,
                        confirmation_prompt=True,
                    )
            if keychain_password:
                pw_file.parent.mkdir(parents=True, exist_ok=True)
                pw_file.write_text(keychain_password)
                pw_file.chmod(0o600)
                click.echo(f"   Stashed keychain password at {pw_file}")

    def prompt() -> tuple[str, str]:
        name = click.prompt("Git author name", default=dev_id)
        email = click.prompt("Git author email", default=f"{dev_id}@devbrain.local")
        return name, email

    outcomes = dev_login.login_dev(
        dev_id,
        cli_names_resolved,
        db=db,
        git_name=git_name,
        git_email=git_email,
        prompt_identity=prompt,
    )

    failures = 0
    for o in outcomes:
        if o.success:
            mark = "✅"
            click.echo(f"{mark} {o.dev_id} → {o.cli_name}")
        else:
            failures += 1
            click.echo(f"❌ {o.dev_id} → {o.cli_name}: {o.error}", err=True)
            if o.hint:
                click.echo(f"   Hint: {o.hint}", err=True)

    if failures:
        sys.exit(1)


@cli.command()
@click.option("--dev", "dev_id", default=None, help="Filter by a single dev_id")
def logins(dev_id):
    """Show which devs are logged into which AI CLIs (table view)."""
    import dev_login

    rows = dev_login.list_logins(db=get_db(), dev_id=dev_id)
    if not rows:
        click.echo("No profiles registered.")
        return

    by_dev: dict[str, dict[str, bool]] = {}
    cli_names: list[str] = []
    for r in rows:
        by_dev.setdefault(r.dev_id, {})[r.cli_name] = r.logged_in
        if r.cli_name not in cli_names:
            cli_names.append(r.cli_name)

    header = ["dev_id"] + cli_names
    col_w = [max(len(h), 8) for h in header]
    for did in by_dev:
        col_w[0] = max(col_w[0], len(did))

    line = "  ".join(h.ljust(w) for h, w in zip(header, col_w))
    click.echo(line)
    click.echo("  ".join("-" * w for w in col_w))
    for did, statuses in sorted(by_dev.items()):
        row = [did]
        for c in cli_names:
            row.append("✅" if statuses.get(c) else "❌")
        click.echo("  ".join(s.ljust(w) for s, w in zip(row, col_w)))


@cli.command()
@click.option("--dev", "dev_id", required=True, help="Dev id to log out")
@click.option(
    "--cli", "cli_arg",
    callback=_validate_cli_choice,
    default=None,
    help="Specific CLI to log out (claude, codex, gemini, or 'all'). Omit to remove the entire profile.",
)
@click.confirmation_option(
    prompt="Are you sure?",
    help="Skip confirmation with --yes",
)
def logout(dev_id, cli_arg):
    """Remove a dev's AI CLI credentials (whole profile or per-CLI)."""
    import dev_login

    cli_names = _resolve_cli_names(cli_arg) if cli_arg else None
    dev_login.logout_dev(dev_id, cli_names=cli_names)
    target = ", ".join(cli_names) if cli_names else "entire profile"
    click.echo(f"✅ Logged out {dev_id}: {target}")


@cli.command(name="reset-keychain")
@click.option("--dev", "dev_id", required=True, help="Dev id whose claude keychain to reset")
@click.option(
    "--keychain-password", default=None,
    help="Set a new custom password now (stashed at "
         "<profile>/.claude/.keychain-password). If omitted: next "
         "`devbrain login` prompts for Random/Custom interactively, "
         "or auto-generates a random one non-interactively.",
)
@click.confirmation_option(
    prompt="Reset claude keychain — discards stored OAuth tokens. Continue?",
    help="Skip confirmation with --yes",
)
def reset_keychain(dev_id, keychain_password):
    """Reset (delete + re-stage) the per-dev claude macOS keychain.

    Use when the keychain password is forgotten/lost, the keychain is
    corrupted (claude returns "Not logged in" repeatedly with valid creds),
    or a dev wants to onboard to a different Claude account on the same
    profile.

    This deletes:
      - <profile>/Library/Keychains/login.keychain-db (the keychain file)
      - <profile>/.claude/.keychain-password (the stashed password)

    After reset, run:
      devbrain login --dev <id> --cli claude

    ...to OAuth fresh into a new keychain.
    """
    import profiles

    profiles.validate_dev_id(dev_id)
    profile_dir = profiles.get_profile_dir(dev_id)
    keychain = profile_dir / "Library" / "Keychains" / "login.keychain-db"
    pw_file = profile_dir / ".claude" / ".keychain-password"

    removed = []
    if keychain.exists():
        keychain.unlink()
        removed.append(str(keychain))
    if pw_file.exists():
        pw_file.unlink()
        removed.append(str(pw_file))

    if not removed:
        click.echo(f"No claude keychain found for dev '{dev_id}'. Nothing to reset.")
        return

    click.echo("Removed:")
    for r in removed:
        click.echo(f"  • {r}")

    if keychain_password:
        pw_file.parent.mkdir(parents=True, exist_ok=True)
        pw_file.write_text(keychain_password)
        pw_file.chmod(0o600)
        click.echo(f"\nStashed new keychain password at {pw_file}")

    click.echo()
    click.echo(f"Next: devbrain login --dev {dev_id} --cli claude")
    click.echo("...to OAuth into a fresh keychain.")


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


@cli.command(name="setup-multi-dev")
@click.option("--host", required=True, help="Postgres host")
@click.option("--port", required=True, type=int, help="Postgres port")
@click.option("--database", required=True, help="Database name")
@click.option("--username", required=True, help="DB username")
@click.option(
    "--password", required=True,
    help="DB password (visible in `ps aux` while this command runs — "
         "for unattended installs prefer setting it via a wrapper that "
         "reads from a secret store)",
)
def setup_multi_dev_cmd(host, port, database, username, password):
    """Scripted: point this DevBrain install at a remote/shared Postgres.

    Tests the connection, then writes DEVBRAIN_DATABASE_URL to .env. Exits
    non-zero if the connection test fails — .env is left untouched on error.
    """
    from setup import setup_multi_dev as _setup_multi_dev
    ok = _setup_multi_dev(
        host=host, port=port, database=database,
        username=username, password=password,
        non_interactive=True,
    )
    if not ok:
        sys.exit(1)


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


# Branch name validation — same regex the MCP server uses (mcp-server/src/index.ts).
# Refuses leading "-" / "." (git-flag injection), refspec form (":"),
# and main/master.
_SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,254}$")


def _validate_branch(ctx, param, value):
    if value is None:
        return None
    v = value.strip()
    if not _SAFE_BRANCH_RE.match(v):
        raise click.BadParameter(
            "branch has unsafe characters — only [A-Za-z0-9_./-] allowed, "
            'cannot start with "-" or "."'
        )
    if v.lower() in ("main", "master"):
        raise click.BadParameter(
            "branch must not be main or master — factory operates on feature branches only"
        )
    return v


@cli.command(name="submit")
@click.argument("title")
@click.option(
    "--spec", default=None,
    help="Detailed feature spec. Defaults to TITLE if omitted.",
)
@click.option(
    "--project", default=None,
    help="Project slug. Defaults to $DEVBRAIN_PROJECT.",
)
@click.option(
    "--cli", "assigned_cli", default=None,
    callback=_validate_cli_choice,
    help="AI CLI to use (claude, codex, gemini). Default from config.",
)
@click.option(
    "--priority", default=0, type=int, show_default=True,
    help="Higher = more urgent.",
)
@click.option(
    "--dev", "submitted_by", default=None,
    help="Dev id to attribute job to. Defaults to $DEVBRAIN_DEV_ID, then $USER.",
)
@click.option(
    "--branch", default=None, callback=_validate_branch,
    help="Existing feature branch to continue work on. Refuses main/master.",
)
@click.option(
    "--no-spawn", is_flag=True,
    help="Create the job row but skip spawning the orchestrator (for tests / manual runs).",
)
def submit(title, spec, project, assigned_cli, priority, submitted_by, branch, no_spawn):
    """Submit a feature to the dev factory.

    Creates a queued job and spawns the orchestrator in the background.
    The factory plans, implements, reviews, runs QA, and stages for approval.

    \b
      devbrain submit "Add a no-op test that asserts True"
      devbrain submit "Add login flow" --spec @spec.md --cli claude --dev alice
    """
    project_slug = project or os.environ.get("DEVBRAIN_PROJECT")
    if not project_slug:
        click.echo(
            "Error: --project required (or set DEVBRAIN_PROJECT env var).",
            err=True,
        )
        sys.exit(1)

    dev_id = (
        submitted_by
        or os.environ.get("DEVBRAIN_DEV_ID")
        or os.environ.get("USER")
    )

    # Allow `--spec @path/to/spec.md` to read spec from file.
    spec_text = spec if spec else title
    if spec_text.startswith("@"):
        spec_path = Path(spec_text[1:]).expanduser()
        if not spec_path.is_file():
            click.echo(f"Error: spec file not found: {spec_path}", err=True)
            sys.exit(1)
        spec_text = spec_path.read_text()

    db = get_db()
    try:
        job_id = db.create_job(
            project_slug=project_slug,
            title=title,
            spec=spec_text,
            priority=priority,
            assigned_cli=assigned_cli,
            submitted_by=dev_id,
            branch_name=branch,
        )
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    click.echo(f"✅ Factory job created: {job_id[:8]} ({title})")
    click.echo(
        f"   project={project_slug}  cli={assigned_cli or '(default)'}  "
        f"dev={dev_id or '(none)'}  branch={branch or '(auto)'}"
    )

    if no_spawn:
        python_bin = str(Path(__file__).parent.parent / ".venv" / "bin" / "python")
        factory_runner = str(Path(__file__).parent / "run.py")
        click.echo("   --no-spawn: orchestrator NOT started.")
        click.echo(f"   Run manually: {python_bin} {factory_runner} {job_id}")
        return

    import subprocess
    factory_runner = str(Path(__file__).parent / "run.py")
    python_bin = str(Path(__file__).parent.parent / ".venv" / "bin" / "python")
    try:
        subprocess.Popen(
            [python_bin, factory_runner, job_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        click.echo(f"   Orchestrator spawned. Track with: devbrain status")
    except Exception as e:
        click.echo(f"   ⚠️  Failed to spawn orchestrator: {e}", err=True)
        click.echo(f"   Run manually: {python_bin} {factory_runner} {job_id}")


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
                # devdoctor --fix runs against a known-bad system; pre-flight
                # baseline failures here are expected, not a reason to abort.
                ctx.invoke(
                    rotate_db_password,
                    yes=False,
                    recreate=True,
                    require_all_healthy=False,
                )
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
                    # Recovery flow: dependents may still be unhealthy from
                    # the drift we just fixed. Don't let baseline failures
                    # abort the rotation the operator just opted into.
                    ctx.invoke(
                        rotate_db_password,
                        yes=False,
                        recreate=True,
                        require_all_healthy=False,
                    )
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
                    "   the live DB password, you can re-run with"
                )
                click.echo(
                    f"   {click.style('devbrain rotate-db-password --current-password', fg='cyan')}"
                )
                click.echo(
                    "   (you'll be prompted securely; or set "
                    "DEVBRAIN_CURRENT_DB_PASSWORD for scripted use)."
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
    "current_password_prompt",
    is_flag=True,
    default=False,
    help="Recovery mode: securely prompt for the live DB password "
         "instead of reading from .env/yaml. Use after a manual ALTER "
         "USER left config out of sync with the live DB. Set env var "
         "DEVBRAIN_CURRENT_DB_PASSWORD to skip the prompt for scripted "
         "use. WARNING: never pass the password as a command argument — "
         "it would appear in 'ps aux', shell history, and process "
         "monitoring captures.",
)
@click.option(
    "--skip-dependents",
    is_flag=True,
    default=False,
    help="Bypass the cred_dependents registry + post-reload verification. "
         "Restores the legacy single-step rotation. Use for emergencies "
         "or when rotating in single-user contexts.",
)
@click.option(
    "--require-all-healthy/--no-require-all-healthy",
    default=True,
    help="Abort rotation if any dependent fails the pre-flight baseline "
         "check. Pass --no-require-all-healthy to rotate even when some "
         "dependents are already broken (won't make things worse).",
)
def rotate_db_password(
    yes: bool,
    recreate: bool,
    current_password_prompt: bool,
    skip_dependents: bool,
    require_all_healthy: bool,
) -> None:
    """Rotate the Postgres password, preserving data.

    Generates a new random password, applies it inside the running
    container via ALTER USER, then syncs the new value to .env and
    config/devbrain.yaml. Reloads every dependent process registered in
    factory.cred_dependents and verifies each one re-authenticated.
    On any failure, rolls back atomically — old creds remain
    authoritative. Optionally recreates the container so updated
    docker-compose settings (e.g., loopback-only port binding) take effect.

    Use --current-password when .env/yaml drifted from the live DB
    (typically after a manual ALTER USER). Rotation will authenticate
    with the supplied value and write the new password to both files.
    """
    import secrets
    import subprocess

    import psycopg2

    from config import CONFIG_PATH, DEVBRAIN_HOME, load_config
    from cred_rotate import (
        RotationContext,
        list_dependents,
        rotate_with_dependents,
    )

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
    # Recovery mode: env var > interactive prompt > config. The flag never
    # accepts a value on the command line, so the password cannot leak via
    # `ps aux`, shell history, or process-monitoring captures.
    recovery_password: str | None = None
    if current_password_prompt:
        recovery_password = os.environ.get("DEVBRAIN_CURRENT_DB_PASSWORD")
        if not recovery_password:
            recovery_password = click.prompt(
                "Live DB password (input hidden)",
                hide_input=True,
                confirmation_prompt=False,
            )
    old_password = (
        recovery_password if recovery_password is not None
        else db_cfg.get("password", "")
    )
    env_path = DEVBRAIN_HOME / ".env"
    yaml_path = CONFIG_PATH

    click.echo("DevBrain — rotate database password")
    click.echo("=" * 60)
    click.echo(f"  User:     {db_user}")
    click.echo(f"  Host:     {db_host}:{db_port}")
    click.echo(f"  Database: {db_name}")
    click.echo(f"  .env:     {env_path}")
    click.echo(f"  yaml:     {yaml_path}")
    if recovery_password is not None:
        click.echo("  Source:   --current-password flag (recovery mode)")
    click.echo()

    if not yes and not click.confirm("Rotate the password now?", default=True):
        click.echo("Aborted.")
        return

    # Verify the current password actually works before generating a new one.
    # Use keyword form (host=, port=, ...) so the password never appears in
    # the connection string libpq echoes back in OperationalError messages.
    click.echo("→ Connecting with current password...", nl=False)
    try:
        psycopg2.connect(
            host=db_host, port=db_port,
            user=db_user, password=old_password, dbname=db_name,
            connect_timeout=5,
        ).close()
    except psycopg2.Error as exc:
        click.echo(" ❌")
        hint = (
            "If the config drifted from the live DB (e.g. someone ran "
            "ALTER USER manually), retry with:\n"
            "    devbrain rotate-db-password --current-password\n"
            "(you'll be prompted securely for the live password)\n"
            "Or run 'devbrain devdoctor --fix' to interactively recover."
        )
        raise click.ClickException(
            f"Can't connect with the current password: {exc}\n{hint}"
        )
    click.echo(" ✅")

    new_password = secrets.token_hex(32)
    ctx = RotationContext(
        user=db_user, host=db_host, port=db_port, database=db_name,
        old_password=old_password, env_path=env_path, yaml_path=yaml_path,
    )

    if not skip_dependents:
        deps = list_dependents(cfg)
        click.echo(f"[rotate] Pre-flight: {len(deps)} dependents registered")
    else:
        click.echo("[rotate] --skip-dependents: registry bypassed")

    click.echo("→ Rotating + reloading dependents...")
    result = rotate_with_dependents(
        ctx, new_password,
        config=cfg,
        require_all_healthy=require_all_healthy,
        skip_dependents=skip_dependents,
    )

    if result.get("aborted_baseline"):
        click.echo(
            "[rotate] ABORTED — some dependents are already unhealthy:",
            err=True,
        )
        for c in result["unhealthy"]:
            click.echo(f"  • {c.id}: {c.error}", err=True)
        click.echo(
            "[rotate] Fix them first, or pass --no-require-all-healthy to "
            "rotate anyway.",
            err=True,
        )
        sys.exit(1)

    if result.get("rollback_failed"):
        click.echo(f"[rotate] FAILED — {result['reason']}", err=True)
        click.echo(
            f"[rotate] ROLLBACK ALSO FAILED — {result['rollback_error']}",
            err=True,
        )
        click.echo(
            "[rotate] Live DB password state is UNKNOWN. .env and yaml "
            "still reflect the new password. Check the DB manually before "
            "retrying — do NOT assume old creds are authoritative.",
            err=True,
        )
        sys.exit(2)

    if result.get("rolled_back"):
        click.echo(f"[rotate] FAILED — {result['reason']}", err=True)
        click.echo(
            "[rotate] ROLLING BACK ALTER USER + .env + yaml... done.",
            err=True,
        )
        click.echo("[rotate] Old creds remain authoritative.", err=True)
        for err in result.get("reload_rollback_errors", []):
            click.echo(
                f"[rotate] WARNING: re-reload during rollback failed: {err}",
                err=True,
            )
        for c in result.get("failed", []):
            click.echo(
                f"[rotate] Investigate {c.id} manually before retrying.",
                err=True,
            )
        sys.exit(1)

    # Success: report dependent status.
    for c in result.get("reloaded", []):
        click.echo(f"  • {c.id} ({c.type}): reloaded, verified ✓")
    for c in result.get("manual", []):
        click.echo(
            f"  • {c.id} (manual_restart): MANUAL ACTION REQUIRED — "
            f"please restart this dependent to pick up new creds"
        )
    n_auto = len(result.get("reloaded", []))
    n_manual = len(result.get("manual", []))
    click.echo(
        f"[rotate] DONE — {n_auto} dependents auto-reloaded, "
        f"{n_manual} need manual restart."
    )

    new_conn_kwargs = dict(
        host=db_host, port=db_port,
        user=db_user, password=new_password, dbname=db_name,
    )

    # Optional container recreate so docker-compose.yml changes (e.g.,
    # loopback-only port binding) take effect. Password rotation alone
    # doesn't require this — ALTER USER already applied it.
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
                    psycopg2.connect(**new_conn_kwargs, connect_timeout=1).close()
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
            # upgrade auto-rotates a weak/default password; a stale
            # dependent shouldn't block fixing a known-weak credential.
            ctx.invoke(
                rotate_db_password,
                yes=yes,
                recreate=True,
                require_all_healthy=False,
            )
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


# ─── Onboarding admin commands (Phase 6) ────────────────────────────────────


@cli.command(name="invitations")
@click.option(
    "--status", default=None,
    help="Filter by status (pending/ready/activated/expired/revoked). Default: all.",
)
def invitations_cmd(status):
    """List dev onboarding invitations."""
    from invitations import list_invitations

    rows = list_invitations(get_db(), status=status)
    if not rows:
        click.echo("No invitations.")
        return

    header = ["id", "dev_id", "status", "pk", "tk", "auto", "expires_at", "created_at"]
    fmt = "{:<10}  {:<20}  {:<10}  {:<3} {:<3} {:<5} {:<20}  {:<20}"
    click.echo(fmt.format(*header))
    click.echo("─" * 110)
    for r in rows:
        click.echo(fmt.format(
            r.id[:8],
            r.dev_id[:18],
            r.status,
            "✓" if r.pubkey else "·",
            "✓" if r.oauth_token else "·",
            "✓" if r.auto_activate else "·",
            r.expires_at.strftime("%Y-%m-%d %H:%M") if r.expires_at else "",
            r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
        ))


@cli.command(name="revoke-invite")
@click.argument("invitation_id")
@click.confirmation_option(
    prompt="Revoke this invitation? (any subsequent webhook submissions will be rejected)",
)
def revoke_invite_cmd(invitation_id):
    """Mark an invitation as revoked. Use the short id from `devbrain invitations`."""
    from invitations import revoke_invitation

    if revoke_invitation(get_db(), invitation_id):
        click.echo(f"✅ Invitation {invitation_id[:8]} revoked.")
    else:
        click.echo(
            f"❌ No revocable invitation matched '{invitation_id}'. "
            "Already activated/revoked, or not found.",
            err=True,
        )
        sys.exit(1)


@cli.command(name="accept-invite")
@click.argument("invite_token")
@click.option(
    "--pubkey-path", type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to your SSH public key (defaults to ~/.ssh/id_ed25519.pub if it exists, "
         "else ~/.ssh/id_rsa.pub).",
)
@click.option(
    "--oauth-token", default=None,
    help="Claude Code OAuth token (sk-ant-oat01-...) generated by `claude setup-token`. "
         "Prompted for if not supplied.",
)
def accept_invite_cmd(invite_token, pubkey_path, oauth_token):
    """Complete onboarding from inside SSH (Alice's side, alternative to webhook).

    Equivalent to the webhook submissions but runs locally on the
    Mac Studio. Use this when:
      - The webhook receiver isn't deployed
      - Alice has SSH access (e.g., admin temporarily added a pubkey
        with `setup add-key` so she can SSH and finish setup)
      - Network paths to the webhook are blocked

    The reconciler will pick up the invitation on its next tick.
    """
    from invitations import submit_pubkey, submit_oauth_token, get_invitation_by_token

    db = get_db()
    inv = get_invitation_by_token(db, invite_token)
    if inv is None:
        click.echo(f"❌ Token doesn't match any invitation.", err=True)
        sys.exit(1)
    if inv.is_expired:
        click.echo(f"❌ Invitation expired at {inv.expires_at}.", err=True)
        sys.exit(1)

    # Resolve pubkey path: explicit > ed25519 > rsa.
    if pubkey_path is None:
        for candidate in (Path.home() / ".ssh/id_ed25519.pub", Path.home() / ".ssh/id_rsa.pub"):
            if candidate.exists():
                pubkey_path = candidate
                break
        if pubkey_path is None:
            click.echo(
                "❌ No SSH pubkey found at ~/.ssh/id_ed25519.pub or "
                "~/.ssh/id_rsa.pub. Generate one with:\n"
                "   ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N \"\"\n"
                "...then re-run `devbrain accept-invite`.",
                err=True,
            )
            sys.exit(1)

    pubkey = Path(pubkey_path).read_text().strip()
    click.echo(f"Submitting pubkey from {pubkey_path}...")
    inv = submit_pubkey(db, invite_token, pubkey)
    if inv is None:
        click.echo("❌ Pubkey submission rejected (already submitted, or malformed).", err=True)
        sys.exit(1)
    click.echo(f"  ✓ pubkey accepted (status: {inv.status})")

    # OAuth token
    if not oauth_token:
        click.echo()
        click.echo(
            "Now paste your Claude Code OAuth token. Generate one on a "
            "machine with a browser:\n"
            "   claude setup-token\n"
            "...and copy the sk-ant-oat01-... string it prints.",
        )
        oauth_token = click.prompt(
            "OAuth token", hide_input=True, show_default=False,
        )
    click.echo("Submitting OAuth token...")
    inv = submit_oauth_token(db, invite_token, oauth_token)
    if inv is None:
        click.echo("❌ OAuth token submission rejected.", err=True)
        sys.exit(1)
    click.echo(f"  ✓ oauth-token accepted (status: {inv.status})")

    click.echo()
    click.echo("✅ Onboarding inputs received.")
    if inv.auto_activate:
        click.echo(
            "   The reconciler will activate your account within the next 30 seconds — "
            "watch `devbrain invitations` to see status flip to 'activated'.",
        )
    else:
        click.echo(
            "   This invitation requires manual admin activation. "
            "Patrick will run `devbrain setup activate --dev "
            f"{inv.dev_id}` when ready.",
        )


@cli.command(name="add-key")
@click.option("--dev", "dev_id", required=True, help="Dev id this pubkey belongs to")
@click.option(
    "--pubkey", "pubkey_arg",
    help="Pubkey contents (paste). Mutually exclusive with --pubkey-file.",
)
@click.option(
    "--pubkey-file", type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to a file containing the pubkey. Mutually exclusive with --pubkey.",
)
def add_key_cmd(dev_id, pubkey_arg, pubkey_file):
    """Admin override: stage a pubkey for an invitation (skip webhook).

    Use when the webhook receiver isn't deployed yet, or when the dev
    sent their pubkey out-of-band (Slack DM, etc.) and the admin is
    pasting it manually.
    """
    from invitations import list_invitations

    if not pubkey_arg and not pubkey_file:
        click.echo("Error: --pubkey or --pubkey-file required.", err=True)
        sys.exit(1)
    if pubkey_arg and pubkey_file:
        click.echo("Error: --pubkey and --pubkey-file are mutually exclusive.", err=True)
        sys.exit(1)

    pubkey = pubkey_arg.strip() if pubkey_arg else Path(pubkey_file).read_text().strip()

    # Find the dev's most recent pending/ready invitation.
    invs = [i for i in list_invitations(get_db()) if i.dev_id == dev_id and i.status in ("pending", "ready")]
    if not invs:
        click.echo(
            f"❌ No pending/ready invitation for dev '{dev_id}'. "
            f"Run `devbrain setup add-dev` to stage one first.",
            err=True,
        )
        sys.exit(1)
    inv = invs[0]

    # Direct DB write (bypasses the webhook's submit_pubkey because we
    # don't have the raw token here — the admin has the dev_id).
    db = get_db()
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE devbrain.invitations
            SET pubkey = %s,
                pubkey_received_at = NOW(),
                status = CASE
                    WHEN status = 'pending' AND oauth_token IS NOT NULL THEN 'ready'
                    ELSE status
                END
            WHERE id = %s AND status IN ('pending', 'ready')
            RETURNING status
            """,
            (pubkey, inv.id),
        )
        row = cur.fetchone()
        conn.commit()
    if row is None:
        click.echo(f"❌ Could not update invitation {inv.id[:8]}.", err=True)
        sys.exit(1)
    click.echo(f"✅ Pubkey staged for {dev_id} (invitation {inv.id[:8]}, status: {row[0]}).")
    if row[0] == "ready":
        click.echo("   The reconciler will activate within ~30s.")


@cli.command(name="send-invite")
@click.option("--dev", "dev_id", required=True, help="Dev id to re-send the kit to")
def send_invite_cmd(dev_id):
    """Re-email the onboarding kit for a dev's most recent active invitation.

    Use when the original email bounced, was lost, or SMTP wasn't yet
    configured at `setup add-dev` time. The same kit file (and same
    invitation token) is sent — no new invitation is created.
    """
    from invitations import list_invitations
    from onboarding_email import send_onboarding_email
    from config import DEVBRAIN_HOME

    # Most recent pending/ready invite for this dev (skip activated/revoked).
    invs = [
        i for i in list_invitations(get_db())
        if i.dev_id == dev_id and i.status in ("pending", "ready")
    ]
    if not invs:
        click.echo(
            f"❌ No pending/ready invitation for dev '{dev_id}'. "
            f"Run `devbrain setup add-dev` to issue a new one.",
            err=True,
        )
        sys.exit(1)
    inv = invs[0]
    if not inv.email:
        click.echo(
            f"❌ Invitation has no email address recorded. "
            f"Cannot auto-send; deliver the kit out of band.",
            err=True,
        )
        sys.exit(1)

    kit_path = DEVBRAIN_HOME / "onboarding" / f"{dev_id}-onboard.md"
    if not kit_path.exists():
        click.echo(
            f"❌ Kit file missing: {kit_path}. "
            f"It may have been deleted; re-issue the invitation with `setup add-dev`.",
            err=True,
        )
        sys.exit(1)

    db = get_db()
    dev = db.get_dev(dev_id) if hasattr(db, "get_dev") else {}
    full_name = (dev or {}).get("full_name") or dev_id
    admin_user = os.environ.get("USER") or "your admin"

    sent = send_onboarding_email(
        to_email=inv.email,
        dev_id=dev_id,
        full_name=full_name,
        kit_path=kit_path,
        admin_name=admin_user,
        admin_contact=admin_user,
    )
    if sent:
        click.echo(f"✅ Onboarding kit re-sent to {inv.email}.")
    else:
        click.echo(
            f"❌ Send failed. Check `devbrain devdoctor` for SMTP config status.",
            err=True,
        )
        sys.exit(1)


@cli.command(name="activate")
@click.option("--dev", "dev_id", required=True, help="Dev id to activate")
def activate_cmd(dev_id):
    """Admin override: force-activate a dev's most recent ready invitation.

    Use when the invitation was created with auto_activate=false, or
    when the reconciler is offline and you need to apply manually.
    """
    from invitations import list_invitations
    from onboard_reconciler import _try_activate

    invs = [i for i in list_invitations(get_db(), status="ready") if i.dev_id == dev_id]
    if not invs:
        click.echo(
            f"❌ No 'ready' invitation for dev '{dev_id}'. "
            f"Either auto_activate already fired, or pubkey/token not yet received.",
            err=True,
        )
        sys.exit(1)
    inv = invs[0]
    # Force activate by overriding auto_activate via DB update,
    # then calling the same path the reconciler uses.
    db = get_db()
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.invitations SET auto_activate = TRUE WHERE id = %s",
            (inv.id,),
        )
        conn.commit()
    # Re-fetch to get the updated row.
    inv = next(i for i in list_invitations(db, status="ready") if i.dev_id == dev_id)
    if _try_activate(db, inv):
        click.echo(f"✅ {dev_id} activated.")
    else:
        click.echo(f"❌ Activation failed (see logs).", err=True)
        sys.exit(1)


# ─── Migration command ──────────────────────────────────────────────────────


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


def _print_counts_line(label: str, c: dict, *, dry_run: bool) -> None:
    """Render one summary line for a backfill counts dict.

    Format: ``[backfill] {label}: {scanned} scanned, {inserted} inserted,
    {skipped_dup} dup[, N no_project][, N no_summary][, N batch_failures]
    ({duration_s}s)``. Skip-counters with value 0 are omitted to keep the
    common-case output terse. ``[dry-run]`` prefix replaces ``[backfill]``
    when --dry-run was passed so it's obvious nothing was written.
    """
    prefix = "[dry-run]" if dry_run else "[backfill]"
    extras = []
    if c.get("skipped_no_project", 0):
        extras.append(f", {c['skipped_no_project']} no_project")
    if c.get("skipped_no_summary", 0):
        extras.append(f", {c['skipped_no_summary']} no_summary")
    if c.get("batch_failures", 0):
        extras.append(f", {c['batch_failures']} batch_failures")
    click.echo(
        f"{prefix} {label}: {c['scanned']} scanned, "
        f"{c['inserted']} inserted, {c['skipped_dup']} dup"
        f"{''.join(extras)} ({c['duration_s']}s)"
    )


@cli.command(name="backfill-memory")
@click.option(
    "--dry-run", is_flag=True,
    help="Print counts without inserting any memory rows.",
)
@click.option(
    "--batch-size", type=int, default=1000, show_default=True,
    help="Rows per batch for keyset-paged scans.",
)
@click.option(
    "--only",
    type=click.Choice(
        ["chunks", "decisions", "patterns", "issues", "raw_sessions"]
    ),
    default=None,
    help="Backfill only one legacy table (default: all five).",
)
def backfill_memory_cmd(dry_run: bool, batch_size: int, only: str | None) -> None:
    """Backfill historical legacy rows into devbrain.memory (P2.c).

    Idempotent — re-running is safe; existing memory rows are preserved
    via the partial unique index from migration 011. Run AFTER P2.b
    dual-writes are deployed.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db = get_db()

    try:
        if only is None:
            results = backfill_memory.backfill_all(
                db, batch_size=batch_size, dry_run=dry_run,
            )
            for label, _fn in backfill_memory._BACKFILLS:
                _print_counts_line(label, results[label], dry_run=dry_run)
            _print_counts_line("TOTAL", results["TOTAL"], dry_run=dry_run)
            total_failures = results["TOTAL"]["batch_failures"]
        else:
            fn = {
                "chunks": backfill_memory.backfill_chunks,
                "decisions": backfill_memory.backfill_decisions,
                "patterns": backfill_memory.backfill_patterns,
                "issues": backfill_memory.backfill_issues,
                "raw_sessions": backfill_memory.backfill_raw_sessions,
            }[only]
            counts = fn(db, batch_size=batch_size, dry_run=dry_run)
            _print_counts_line(only, counts, dry_run=dry_run)
            total_failures = counts["batch_failures"]
    except RuntimeError as exc:
        click.echo(f"[backfill] FAILED: {exc}", err=True)
        sys.exit(1)

    if total_failures > 0:
        sys.exit(1)


def _print_attribute_counts_line(label: str, c: dict, *, dry_run: bool) -> None:
    """Render one summary line for an attribute-orphans counts dict.

    Sessions branch carries ``unrecoverable`` and (optionally)
    ``fallback_to_default``; chunks branch carries ``parent_still_null``.
    Counters with value 0 are omitted from the optional section to keep
    the common-case output terse. ``[dry-run]`` prefix replaces
    ``[attribute]`` when --dry-run was passed so it's obvious nothing
    was written.
    """
    prefix = "[dry-run]" if dry_run else "[attribute]"
    if "unrecoverable" in c:
        body = (
            f"{c['scanned']} scanned, {c['attributed']} attributed, "
            f"{c['unrecoverable']} unrecoverable"
        )
        if c.get("fallback_to_default", 0):
            body += f", {c['fallback_to_default']} fallback_to_default"
    else:
        body = (
            f"{c['scanned']} scanned, {c['attributed']} attributed, "
            f"{c['parent_still_null']} parent_still_null"
        )
    if c.get("batch_failures", 0):
        body += f", {c['batch_failures']} batch_failures"
    click.echo(f"{prefix} {label:<8}: {body} ({c['duration_s']}s)")


@cli.command(name="attribute-orphans")
@click.option(
    "--dry-run", is_flag=True,
    help="Print counts without writing project_id to any row.",
)
@click.option(
    "--batch-size", type=int, default=1000, show_default=True,
    help="Rows per batch for keyset-paged scans.",
)
@click.option(
    "--default-project", "default_project_slug", default=None,
    help="Fallback project slug for rows whose source_path can't be "
         "decoded or matches no configured project. Off by default; "
         "fails loud if the slug isn't in devbrain.projects.",
)
def attribute_orphans_cmd(
    dry_run: bool, batch_size: int, default_project_slug: str | None,
) -> None:
    """Attribute orphan claude_code raw_sessions + chunks via source_path.

    Decodes claude_code source_paths back to project directories and
    matches them against ``factory.project_paths`` in devbrain.yaml.
    Idempotent — re-running is safe; the SELECT filters
    ``project_id IS NULL`` and the UPDATE re-asserts the same predicate.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db = get_db()

    try:
        results = attribute_orphans.attribute_all(
            db,
            batch_size=batch_size,
            dry_run=dry_run,
            default_project_slug=default_project_slug,
        )
    except (RuntimeError, ValueError) as exc:
        click.echo(f"[attribute] FAILED: {exc}", err=True)
        sys.exit(1)

    _print_attribute_counts_line("sessions", results["sessions"], dry_run=dry_run)
    _print_attribute_counts_line("chunks", results["chunks"], dry_run=dry_run)

    total_failures = (
        results["sessions"]["batch_failures"]
        + results["chunks"]["batch_failures"]
    )
    if not dry_run:
        click.echo(
            "[attribute] DONE — re-run `devbrain backfill-memory` to "
            "migrate the newly-attributed rows."
        )

    if total_failures > 0:
        sys.exit(1)


@cli.command(name="export-memory")
@click.option(
    "--out", "out_path", type=click.Path(path_type=Path), required=True,
    help="Output file (use .gz suffix for gzip).",
)
@click.option(
    "--project", "project_slugs", multiple=True,
    help="Export only this project slug. Repeatable. Default: every project.",
)
@click.option(
    "--gzip/--no-gzip", "gzip_output", default=None,
    help="Force gzip on/off. Default: infer from --out suffix.",
)
def export_memory_cmd(
    out_path: Path,
    project_slugs: tuple[str, ...],
    gzip_output: bool | None,
) -> None:
    """Export devbrain.memory + raw_sessions + projects + devs to a file.

    Pairs with `import-memory` for cross-machine migration. The export
    captures everything needed to recreate this DevBrain instance's
    accumulated memory on another machine — see docs/MIGRATING.md for
    the full operator playbook.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db = get_db()
    try:
        counts = export_memory.write_export_file(
            db,
            out_path,
            project_slugs=project_slugs or None,
            database_url=DATABASE_URL,
            gzip_output=gzip_output,
        )
    except Exception as exc:
        # Catch broadly so DB-down (psycopg2.OperationalError) or other
        # driver errors surface as "[export] FAILED: …" instead of an
        # uncaught traceback.
        click.echo(f"[export] FAILED: {exc}", err=True)
        sys.exit(1)

    click.echo(
        f"[export] wrote {out_path}: "
        f"projects={counts['projects']}, devs={counts['devs']}, "
        f"memory={counts['memory']}, raw_sessions={counts['raw_sessions']}"
    )


@cli.command(name="import-memory")
@click.option(
    "--in", "in_path", type=click.Path(exists=True, path_type=Path),
    required=True,
    help="Export file to read (auto-detects .gz).",
)
@click.option(
    "--dry-run", is_flag=True,
    help="Read the file and report counts without committing changes.",
)
def import_memory_cmd(in_path: Path, dry_run: bool) -> None:
    """Import a `export-memory` payload into this DevBrain instance.

    Idempotent: re-running on the same file is safe. Existing local
    rows are preserved (slug-keyed projects, dev_id-keyed devs,
    `(provenance_id, kind)` memory rows, `(source_app, source_hash)`
    raw_sessions). Locally-customized notification channels survive
    a re-import — only previously-unknown devs are inserted.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    db = get_db()
    try:
        payload = import_memory.read_import_file(in_path)
    except Exception as exc:
        click.echo(f"[import] FAILED to read {in_path}: {exc}", err=True)
        sys.exit(1)

    try:
        results = import_memory.import_from_dict(
            db, payload, dry_run=dry_run,
        )
    except Exception as exc:
        # Catch broadly so DB-down (psycopg2.OperationalError) or
        # IntegrityError on a future NOT NULL column surface as
        # "[import] FAILED: …" instead of an uncaught traceback.
        click.echo(f"[import] FAILED: {exc}", err=True)
        sys.exit(1)

    prefix = "[dry-run]" if dry_run else "[import]"
    click.echo(
        f"{prefix} projects: {results['projects']['count']} resolved"
    )
    click.echo(
        f"{prefix} devs: {results['devs']['inserted']} inserted, "
        f"{results['devs']['preserved']} preserved"
    )
    click.echo(
        f"{prefix} raw_sessions: {results['raw_sessions']['inserted']} "
        f"inserted, {results['raw_sessions']['skipped_dup']} dup "
        f"(of {results['raw_sessions']['scanned']} scanned)"
    )
    mem_msg = (
        f"{prefix} memory: {results['memory']['inserted']} inserted, "
        f"{results['memory']['skipped_dup']} dup "
        f"(of {results['memory']['scanned']} scanned)"
    )
    no_slug = results["memory"].get("skipped_no_slug", 0)
    if no_slug:
        mem_msg += f", {no_slug} skipped (no project_slug)"
    click.echo(mem_msg)


# ---------------------------------------------------------------------------
# Curator subcommands — cascade re-eval queue operations.
# ---------------------------------------------------------------------------

@cli.group()
def curator():
    """Curator agent + cascade queue operations."""


@curator.command("queue-stuck")
def cmd_queue_stuck():
    """List re-eval queue rows that failed 3+ times.

    These rows have exhausted their retry budget and are skipped by the
    drain worker. Operator triage: investigate `last_error`, then either
    fix the underlying problem and DELETE the row (a fresh enqueue is
    permitted because the dedup partial unique index excludes
    attempt_count >= 3 rows), or accept the stale strength.
    """
    from curator.cli import list_stuck_queue_rows

    db = get_db()
    with db._conn() as conn:
        rows = list_stuck_queue_rows(conn)
    if not rows:
        click.echo("No stuck rows.")
        return
    for r in rows:
        click.echo(
            f"{r['id']}  memory={r['memory_id']}  "
            f"src={r['cascade_source_id']}  edge={r['edge_type']}  "
            f"attempts={r['attempt_count']}  err={r['last_error']!r}"
        )


# ---------------------------------------------------------------------------
# Compliance-rule subcommands — the lint check enforces that every
# profile-tagged rule ships with a postulate test (Atlas Step 7b).
# ---------------------------------------------------------------------------

@cli.group()
def rules():
    """Compliance rule operations."""


@rules.command("lint")
def cmd_rules_lint():
    """Verify every profile-tagged rule has a matching postulate test.

    A rule is "profile-tagged" if its compliance_profiles array is
    non-empty. The lint heuristic: scan tests/postulates/*.py for either
    the rule's UUID or a slugified title token. Exits 1 if any tagged
    rule has no matching postulate, 0 otherwise.
    """
    from curator.rules_lint import run_lint

    db = get_db()
    with db._conn() as conn:
        sys.exit(run_lint(conn))


# ─── cognify commands ─────────────────────────────────────────────────────────


@cli.command("cognify")
@click.option(
    "--pass",
    "pass_name",
    default=None,
    help="Pass to run: extract | decay | edges | strengthen | gc",
)
@click.option("--all", "run_all", is_flag=True, default=False,
              help="Run all passes in dependency order.")
@click.option(
    "--project",
    "project_slug",
    default=None,
    help="Project slug to scope the pass (required for LLM passes).",
)
@click.option("--dry-run", is_flag=True, default=False,
              help="Report what the pass would do without making changes.")
@click.option("--cross-project", "cross_project", is_flag=True, default=False,
              help="Edges pass only: include canonical 'devbrain' rules library "
                   "as candidate-pair source. Detects when project lessons "
                   "contradict canonical regulatory rules. Other passes ignore.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit result as JSON.")
def cognify_command(pass_name, run_all, project_slug, dry_run, cross_project, as_json):
    """Run one or all cognify passes.

    Examples:\n
      devbrain cognify --pass=decay\n
      devbrain cognify --pass=extract --project=myproject\n
      devbrain cognify --all --project=myproject\n
      devbrain cognify --pass=decay --dry-run\n
      devbrain cognify --pass=edges --project=brightbot --cross-project
    """
    import sys
    from config import DATABASE_URL
    import psycopg2
    import psycopg2.extras
    psycopg2.extras.register_uuid()

    if not pass_name and not run_all:
        raise click.UsageError("Specify --pass=<name> or --all")

    db = get_db()
    project_id = None
    if project_slug:
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM devbrain.projects WHERE slug = %s",
                (project_slug,),
            )
            row = cur.fetchone()
        if not row:
            raise click.ClickException(f"Project {project_slug!r} not found.")
        project_id = row[0]

    # Import cognify modules (adds factory/ to sys.path).
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    from cognify.orchestrator import run_pass as _run_pass, run_all as _run_all

    with db._conn() as conn:
        if run_all:
            results = _run_all(
                conn, project_id, dry_run=dry_run, cross_project=cross_project
            )
            if as_json:
                import json as _json
                click.echo(_json.dumps(
                    {k: vars(v) for k, v in results.items()}, indent=2, default=str
                ))
            else:
                for pname, res in results.items():
                    status = "dry-run" if dry_run else "ok"
                    click.echo(
                        f"  {pname:12s}  rows={res.rows_processed}  "
                        f"llm={res.llm_calls}  [{status}]"
                    )
        else:
            result = _run_pass(
                conn, pass_name, project_id,
                dry_run=dry_run, cross_project=cross_project,
            )
            if as_json:
                import json as _json
                click.echo(_json.dumps(vars(result), indent=2, default=str))
            else:
                status = "dry-run" if dry_run else "ok"
                click.echo(
                    f"cognify {pass_name}: rows={result.rows_processed}  "
                    f"llm={result.llm_calls}  [{status}]"
                )


@cli.command("cognify-reextract")
@click.option("--session", "session_id", default=None,
              help="Re-extract a single session by provenance_id.")
@click.option("--all", "all_sessions", is_flag=True, default=False,
              help="Re-extract all sessions for the project.")
@click.option("--since", default=None,
              help="Re-extract sessions ingested after this ISO date.")
@click.option("--project", "project_slug", required=True,
              help="Project slug.")
@click.option("--dry-run", is_flag=True, default=False,
              help="Report what would be re-extracted without making changes.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Emit result as JSON.")
def cognify_reextract_command(
    session_id, all_sessions, since, project_slug, dry_run, as_json
):
    """Re-extract lessons/decisions for one or all sessions.

    Archives prior extracted rows (sets archived_at; never deletes).
    New rows carry metadata.reextracted_from for traceability.

    Examples:\n
      devbrain cognify-reextract --session=abc123 --project=myproject\n
      devbrain cognify-reextract --all --project=myproject\n
      devbrain cognify-reextract --since=2026-04-01 --project=myproject
    """
    import sys

    db = get_db()
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM devbrain.projects WHERE slug = %s",
            (project_slug,),
        )
        row = cur.fetchone()
    if not row:
        raise click.ClickException(f"Project {project_slug!r} not found.")
    project_id = row[0]

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
    from cognify.reextract_cli import run_reextract

    with db._conn() as conn:
        result = run_reextract(
            conn,
            project_id,
            session_id=session_id,
            all_sessions=all_sessions,
            since=since,
            dry_run=dry_run,
        )

    if as_json:
        import json as _json
        click.echo(_json.dumps(result, indent=2, default=str))
    else:
        if dry_run:
            click.echo(
                f"dry-run: would re-extract {result.get('sessions_targeted', 0)} session(s)"
            )
        else:
            click.echo(
                f"Re-extracted {result.get('sessions_targeted', 0)} session(s): "
                f"lessons={result.get('lessons_created', 0)}  "
                f"decisions={result.get('decisions_created', 0)}"
            )


if __name__ == "__main__":
    cli()
