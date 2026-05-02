"""Send the onboarding kit to a new dev via SMTP.

Reuses the SmtpChannel from factory/notifications/channels/smtp.py so
the SMTP credentials only have to be configured in one place
(config/devbrain.yaml under notifications.channels.smtp).

Email body has three parts: a short welcome paragraph, ~5 step-summary
bullets, and the full onboarding kit Markdown inlined below a divider.
The dev (or their AI agent) drops the bottom section into a `.md` file
or pastes directly into Claude Code / Codex / etc.

Why inline (not attachment):
  - Most agent UIs accept pasted Markdown more reliably than .md
    attachments.
  - Removes attachment-related deliverability issues (Gmail blocks
    .md attachments by default in some configurations).
  - Keeps the email a single MIME part; works in plain-text-only
    email clients too.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Email body template. Patrick reviews + tweaks via the optional
# template-override path documented below before the first real send.
_EMAIL_TEMPLATE = """Welcome to BrightBot, {first_name}!

You've been invited to join the BrightBot dev factory — Lighthouse
Therapy's multi-AI-agent automation pipeline that drafts, implements,
reviews, and QAs feature work using YOUR Claude / Codex / Gemini
subscriptions, attributed to YOUR git identity.

# How to onboard (5 minutes)

1. Drop the kit below into Claude Code, Codex, or your AI agent of
   choice. The agent walks through every step, asking for your
   approval as it goes.
   (Or run the steps manually — every command is shown in the kit.)

2. The agent generates an SSH keypair, installs Claude Code locally,
   and prompts you to authorize via your browser (one OAuth dance).

3. You paste the resulting OAuth token (sk-ant-oat01-...) when the
   agent asks. The token transits ONCE, scoped to one URL, never
   logged.

4. DevBrain auto-activates your account. You'll get a confirmation.

5. SSH into the Mac Studio (`ssh mac-studio`) and run
   `factory dashboard` — you're in.

The invite token below is single-use and expires in 7 days. Treat the
kit like a credential — don't broadcast it.

If anything breaks, reply to this email or ping {admin_contact}.

— DevBrain (on behalf of {admin_name})

────────────────────────────────────────────────────────────────────
─── ONBOARDING KIT (drop everything below into your AI agent) ──────
────────────────────────────────────────────────────────────────────

{kit_content}
"""


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
) -> bool:
    """Send the onboarding kit to the dev.

    Picks the configured email channel (gmail_dwd preferred, smtp
    fallback). Returns True on successful delivery, False otherwise.
    Failures are logged and the kit file is left in place — the admin
    can re-send via `devbrain send-invite --dev <id>` after fixing
    config.
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
    kit_content = kit_path.read_text()

    body = _EMAIL_TEMPLATE.format(
        first_name=first_name,
        admin_name=admin_name,
        admin_contact=admin_contact,
        kit_content=kit_content,
    )

    title = "Welcome to BrightBot — your DevBrain onboarding kit"

    result = channel.send(address=to_email, title=title, body=body)
    if not result.delivered:
        logger.error("Email send failed via %s: %s", channel.name, result.error)
        return False
    logger.info(
        "Onboarding email sent to %s for dev=%s via %s",
        to_email, dev_id, channel.name,
    )
    return True
