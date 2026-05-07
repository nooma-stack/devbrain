"""Send the onboarding kit to a new dev via email.

Reuses the SmtpChannel or GmailDwdChannel from
factory/notifications/channels/ so the credentials only have to be
configured in one place (config/devbrain.yaml under
notifications.channels.smtp or notifications.channels.gmail_dwd).

Email body branches on `agent_app`:
- specified (claude-desktop / codex-desktop / etc.): tailored "drop
  the .md into [agent app]" instructions.
- auto / unknown: lists supported agent apps with install pointers,
  letting the dev pick.

The full kit content is always delivered as a .md attachment (not
inlined). The attachment is ~10-15 KB — well within any provider's
limits — and is trivially saved to disk for the dev to feed to their
AI agent.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_CLI_DISPLAY_NAMES: dict[str, str] = {
    "claude": "Claude Code",
    "codex":  "Codex",
    "gemini": "Gemini CLI",
}

_AGENT_APP_DISPLAY_NAMES: dict[str, str] = {
    "auto":            "your AI agent",
    "claude-desktop":  "Claude Desktop",
    "codex-desktop":   "Codex Desktop",
    "gemini-desktop":  "Gemini Desktop",
    "claude-cli":      "Claude Code (CLI)",
    "codex-cli":       "Codex (CLI)",
    "gemini-cli":      "Gemini (CLI)",
}

# Per-agent-app: how to drop the kit into the agent
_AGENT_APP_DROP_INSTRUCTIONS: dict[str, str] = {
    "claude-desktop": (
        "Open Claude Desktop, attach the {kit_filename} file to your "
        "next message (paperclip icon), and tell Claude: \"Please run "
        "this onboarding kit.\""
    ),
    "codex-desktop": (
        "Open Codex Desktop, attach the {kit_filename} file to your "
        "next message, and tell Codex: \"Please run this onboarding "
        "kit.\""
    ),
    "gemini-desktop": (
        "Open Gemini Desktop, attach the {kit_filename} file to your "
        "next message, and tell Gemini: \"Please run this onboarding "
        "kit.\""
    ),
    "claude-cli": (
        "Open a terminal, run `claude`, and paste the contents of "
        "{kit_filename} (or pass it as a path argument: "
        "`claude < {kit_filename}`)."
    ),
    "codex-cli": (
        "Open a terminal, run `codex`, and paste the contents of "
        "{kit_filename}."
    ),
    "gemini-cli": (
        "Open a terminal, run `gemini`, and paste the contents of "
        "{kit_filename}."
    ),
}

# Agent-app-agnostic preface for "auto" mode
_AUTO_AGENT_APP_PREFACE = """\
**If you don't have an AI agent app yet,** install one of the
following (any of them can run this kit):

- **Claude Desktop** — https://claude.com/download
- **Codex Desktop** — https://chatgpt.com/download (includes Codex)
- **Gemini Desktop** — https://gemini.google.com/app

Once installed, drop the attached {kit_filename} into the app and
tell the agent: \"Please run this onboarding kit.\"

If you'd rather use a CLI: `claude`, `codex`, or `gemini` will all
work the same way — just paste the kit's contents at the prompt.
"""


_EMAIL_TEMPLATE = """Welcome to BrightBot, {first_name}!

You've been invited to join the BrightBot dev factory — Lighthouse
Therapy's multi-AI-agent automation pipeline that drafts, implements,
reviews, and QAs feature work using YOUR {cli_display_name} subscription,
attributed to YOUR git identity.

# How to onboard (~5 minutes)

{drop_instruction}

The kit asks for your approval at each step. It generates an SSH
keypair on your machine, delivers the public half to the Mac Studio,
then SSHes you into your profile on the Mac Studio so you can sign
into your {cli_display_name} subscription server-side. Your auth
token is generated on the Mac Studio and **never leaves it** — only
your SSH public key transits this email's chain.

The bootstrap key in the attached kit is single-use, locked to a
single rotation script, and auto-expires in 3 days. Treat the kit
like a credential — don't broadcast it.

If anything breaks, reply to this email or ping {admin_contact}.

— DevBrain (on behalf of {admin_name})
"""


def _pick_email_channel():
    """Return an instantiated, configured email channel, or None.

    Preference order:
      1. gmail_dwd  (Google Workspace service account; no password)
      2. smtp       (plain SMTP / Gmail App Password / etc.)
    """
    import sys
    from pathlib import Path as _Path
    sys.path.insert(0, str(_Path(__file__).resolve().parent))

    from config import NOTIFICATIONS_CONFIG

    channels = NOTIFICATIONS_CONFIG.get("channels", {})

    dwd_cfg = channels.get("gmail_dwd", {}) or {}
    if dwd_cfg.get("enabled"):
        from notifications.channels.gmail_dwd import GmailDwdChannel
        kwargs = {k: v for k, v in dwd_cfg.items() if k != "enabled"}
        ch = GmailDwdChannel(**kwargs)
        if ch.is_configured():
            return ch
        logger.warning(
            "gmail_dwd channel enabled but not configured (missing "
            "service_account_path / sender_email, or google-auth + "
            "google-api-python-client not installed)."
        )

    smtp_cfg = channels.get("smtp", {}) or {}
    if smtp_cfg.get("enabled"):
        from notifications.channels.smtp import SmtpChannel
        kwargs = {k: v for k, v in smtp_cfg.items() if k != "enabled"}
        ch = SmtpChannel(**kwargs)
        if ch.is_configured():
            return ch
        logger.warning(
            "smtp channel enabled but not configured (missing host / sender_email)."
        )

    return None


def _build_drop_instruction(agent_app: str, kit_filename: str) -> str:
    """Return the agent-app-specific 'drop the kit into your agent' block."""
    if agent_app == "auto":
        return _AUTO_AGENT_APP_PREFACE.format(kit_filename=kit_filename)
    template = _AGENT_APP_DROP_INSTRUCTIONS.get(agent_app)
    if template is None:
        # Defensive fallback — shouldn't happen if the validator runs first
        return _AUTO_AGENT_APP_PREFACE.format(kit_filename=kit_filename)
    rendered = template.format(kit_filename=kit_filename)
    return f"**To get started:** {rendered}"


def send_onboarding_email(
    *,
    to_email: str,
    dev_id: str,
    full_name: str,
    kit_path: Path,
    admin_name: str = "your admin",
    admin_contact: str = "the admin who sent this email",
    cli: str = "claude",
    agent_app: str = "auto",
) -> bool:
    """Send the onboarding kit to the dev as a .md email attachment.

    Args:
        cli: AI subscription the dev was invited for. Personalises
             body wording.
        agent_app: Which AI agent app the dev will use to run the
                   kit. When 'auto', the email lists install
                   pointers for each supported app and lets the dev
                   pick. Otherwise, the email gives tailored
                   drop-in instructions for that specific app.
    """
    channel = _pick_email_channel()
    if channel is None:
        logger.warning(
            "No email channel enabled (gmail_dwd or smtp). Configure via "
            "`devbrain setup channels`, then re-run "
            "`devbrain send-invite --dev <id>` to retry the send."
        )
        return False

    first_name = full_name.split()[0] if full_name else dev_id
    cli_display_name = _CLI_DISPLAY_NAMES.get(cli, cli)
    kit_filename = f"{dev_id}-onboarding-kit.md"
    drop_instruction = _build_drop_instruction(agent_app, kit_filename)

    body = _EMAIL_TEMPLATE.format(
        first_name=first_name,
        cli_display_name=cli_display_name,
        drop_instruction=drop_instruction,
        admin_name=admin_name,
        admin_contact=admin_contact,
    )

    title = f"Welcome to BrightBot — your DevBrain onboarding kit ({cli_display_name})"

    # Rename the kit file to the canonical attachment name if it differs.
    if kit_path.name != kit_filename:
        attachment_path = kit_path.parent / kit_filename
        attachment_path.write_bytes(kit_path.read_bytes())
        attachment_path.chmod(0o600)
    else:
        attachment_path = kit_path

    result = channel.send(
        address=to_email,
        title=title,
        body=body,
        attachments=[attachment_path],
    )
    if not result.delivered:
        logger.error("Email send failed via %s: %s", channel.name, result.error)
        return False
    logger.info(
        "Onboarding email sent to %s for dev=%s (cli=%s, agent_app=%s) via %s; "
        "kit attached as %s",
        to_email, dev_id, cli, agent_app, channel.name, kit_filename,
    )
    return True
