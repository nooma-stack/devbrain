"""SMTP notification channel.

Works with any SMTP provider (Gmail app passwords, Fastmail, Mailgun, SES, etc.).
Supports STARTTLS and pulls credentials from config or environment variables.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from notifications.base import ChannelResult, NotificationChannel, default_registry

logger = logging.getLogger(__name__)


class SmtpChannel(NotificationChannel):
    """Send notifications via an SMTP server."""

    name = "smtp"

    def __init__(
        self,
        host: str = "",
        port: int = 587,
        use_tls: bool = True,
        username: str = "",
        password: str = "",
        sender_email: str = "",
        sender_display_name: str = "DevBrain",
        **kwargs,
    ):
        super().__init__(
            host=host,
            port=port,
            use_tls=use_tls,
            username=username,
            password=password,
            sender_email=sender_email,
            sender_display_name=sender_display_name,
            **kwargs,
        )
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self.username = username
        self.password = password
        self.sender_email = sender_email
        self.sender_display_name = sender_display_name

    def _username(self) -> str:
        return self.username or os.environ.get("SMTP_USERNAME", "")

    def _password(self) -> str:
        return self.password or os.environ.get("SMTP_PASSWORD", "")

    def is_configured(self) -> bool:
        return bool(self.host) and bool(self.sender_email)

    def send(self, address: str, title: str, body: str, **kwargs) -> ChannelResult:
        try:
            msg = MIMEMultipart()
            msg["Subject"] = title
            msg["From"] = f"{self.sender_display_name} <{self.sender_email}>"
            msg["To"] = address
            msg.attach(MIMEText(body, "plain"))

            with smtplib.SMTP(self.host, self.port, timeout=30) as server:
                if self.use_tls:
                    server.starttls()
                username = self._username()
                if username:
                    server.login(username, self._password())
                server.send_message(msg)

            return ChannelResult(delivered=True, channel=self.name)
        except smtplib.SMTPAuthenticationError as e:
            logger.error("SMTP auth failure: %s", e)
            return ChannelResult(
                delivered=False,
                channel=self.name,
                error=f"SMTP auth error: {e}",
            )
        except Exception as e:
            logger.error("SMTP send failure: %s", e)
            return ChannelResult(
                delivered=False,
                channel=self.name,
                error=f"{type(e).__name__}: {e}",
            )


default_registry.register("smtp", SmtpChannel)
