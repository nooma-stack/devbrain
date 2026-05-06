"""Send the onboarding kit to a new dev via email.

Reuses the SmtpChannel or GmailDwdChannel from
factory/notifications/channels/ so the credentials only have to be
configured in one place (config/devbrain.yaml under
notifications.channels.smtp or notifications.channels.gmail_dwd).

Email body: a short welcome paragraph + 5-step summary + a pointer to
the attached kit file. The full kit content is delivered as a .md
attachment (not inlined). The attachment is ~3-15 KB — well within any
provider's limits — and is trivially saved to disk for the dev to feed
to their AI agent.

Why attachment (not inline):
  - AI agent UIs (Claude Code, Codex, Gemini CLI) all accept file paths
    on the command line; dragging/pasting a saved .md is the natural
    onboarding path.
  - Keeps the visible email body short and scan-friendly for the human.
  - Avoids the body growing with future kit expansions (multi-CLI kits
    can get long; the email subject+body stay constant).
  - Re-send is trivial: the attachment is the same kit file on disk.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Email body template. Short by design — full content is in the attachment.
# Patrick reviews + tweaks via the optional template-override path documented
# below before the first real send.
_EMAIL_TEMPLATE = """Welcome to BrightBot, {first_name}!

You've been invited to join the BrightBot dev factory — Lighthouse
Therapy's multi-AI-agent automation pipeline that drafts, implements,
reviews, and QAs feature work using YOUR {cli_display_name} subscription,
attributed to YOUR git identity.

# How to onboard (~5 minutes)

1. Save the attached file ({kit_filename}) to your laptop.

2. Drop it into your AI agent ({cli_display_name}, or any agent you prefer).
   The agent walks through every step, asking for your approval as it goes.
   (Or run the steps manually — every command is shown in the kit.)

3. The agent generates an SSH keypair, installs {cli_display_name} locally,
   and prompts you to authorize your credentials.

4. DevBrain auto-activates your account. You'll get a confirmation.

5. SSH into the Mac Studio (`ssh mac-studio`) and run
   `factory dashboard` — you're in.

The invite token in the kit is single-use and expires in 7 days. Treat
the kit like a credential — don't broadcast it.

If anything breaks, reply to this email or ping {admin_contact}.

— DevBrain (on behalf of {admin_name})
"""

_CLI_DISPLAY_NAMES: dict[str, str] = {
    "claude": "Claude Code",
    "codex":  "Codex",
    "gemini": "Gemini CLI",
}


def _pick_email_channel():
    """Return an instantiated, configured email channel, or None.

    Preference order:
      1. gmail_dwd  (Google Workspace service account; no password)
      2. smtp       (plain SMTP / Gmail App Password / etc.)

    Only one is expected to be enabled at a time — the setup wizard
    disables the other when one is configured. But if both are
    enabled, gmail_dwd wins because it's password-rotation-free and
    audit-friendly.
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


def send_onboarding_email(
    *,
    to_email: str,
    dev_id: str,
    full_name: str,
    kit_path: Path,
    admin_name: str = "your admin",
    admin_contact: str = "the admin who sent this email",
    cli: str = "claude",
) -> bool:
    """Send the onboarding kit to the dev as a .md email attachment.

    The email body is a short welcome + 5-step summary. The full kit
    content is delivered as an attachment named `<dev_id>-onboarding-kit.md`.

    Picks the configured email channel (gmail_dwd preferred, smtp fallback).
    Returns True on successful delivery, False otherwise. Failures are
    logged and the kit file is left in place — the admin can re-send via
    `devbrain send-invite --dev <id>` after fixing config.

    Args:
        cli: AI CLI the dev was invited for. Used to personalise the email
             body (e.g. "your Codex subscription"). Defaults to 'claude'.
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

    body = _EMAIL_TEMPLATE.format(
        first_name=first_name,
        cli_display_name=cli_display_name,
        kit_filename=kit_filename,
        admin_name=admin_name,
        admin_contact=admin_contact,
    )

    title = f"Welcome to BrightBot — your DevBrain onboarding kit ({cli_display_name})"

    # Rename the kit file to the canonical attachment name if it differs.
    # This avoids surprising filenames (e.g. "alice-onboard.md") when the
    # attachment lands in the dev's inbox. We use a symlink-free rename
    # approach: write a copy with the canonical name, send that, leave the
    # original in place (the admin's reference copy).
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
        "Onboarding email sent to %s for dev=%s (cli=%s) via %s; "
        "kit attached as %s",
        to_email, dev_id, cli, channel.name, kit_filename,
    )
    return True
