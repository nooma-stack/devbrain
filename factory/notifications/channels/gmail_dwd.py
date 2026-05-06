"""Gmail notification channel using a Google service account with DWD.

Sends mail via the Gmail API by impersonating ``sender_email`` using a
service account that has domain-wide delegation enabled for the
``gmail.send`` scope.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
from email.mime.application import MIMEApplication
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from notifications.base import ChannelResult, NotificationChannel, default_registry

logger = logging.getLogger(__name__)

# Lazy import of google libs — channel stays importable without deps installed
try:
    from google.oauth2 import service_account  # type: ignore
    from googleapiclient.discovery import build  # type: ignore

    GOOGLE_AVAILABLE = True
except ImportError:  # pragma: no cover - only hit when deps are missing
    service_account = None  # type: ignore
    build = None  # type: ignore
    GOOGLE_AVAILABLE = False


GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"


class GmailDwdChannel(NotificationChannel):
    """Send notifications via Gmail API using a DWD service account."""

    name = "gmail_dwd"

    def __init__(
        self,
        service_account_path: str = "",
        sender_email: str = "",
        sender_display_name: str = "DevBrain",
        **kwargs,
    ):
        super().__init__(
            service_account_path=service_account_path,
            sender_email=sender_email,
            sender_display_name=sender_display_name,
            **kwargs,
        )
        self.service_account_path: Path | None = (
            Path(service_account_path).expanduser() if service_account_path else None
        )
        self.sender_email = sender_email
        self.sender_display_name = sender_display_name
        self._service = None

    def is_configured(self) -> bool:
        if not GOOGLE_AVAILABLE:
            return False
        if not self.service_account_path:
            return False
        if not self.service_account_path.exists():
            return False
        if not self.sender_email:
            return False
        return True

    def _get_service(self):
        if self._service is not None:
            return self._service

        credentials = service_account.Credentials.from_service_account_file(
            str(self.service_account_path),
            scopes=[GMAIL_SEND_SCOPE],
        ).with_subject(self.sender_email)

        self._service = build("gmail", "v1", credentials=credentials)
        return self._service

    def send(
        self,
        address: str,
        title: str,
        body: str,
        attachments: Optional[list[Path]] = None,
        **kwargs,
    ) -> ChannelResult:
        """Send an email, optionally with file attachments.

        Args:
            address: Recipient email address.
            title: Email subject.
            body: Plain-text email body.
            attachments: Optional list of Path objects to attach. Each file
                is attached using its basename as the Content-Disposition
                filename. Files are encoded as base64 per the Gmail API's
                multipart/mixed requirements.
        """
        if not self.is_configured():
            return ChannelResult(
                delivered=False,
                channel=self.name,
                error="gmail_dwd channel is not configured",
            )

        try:
            msg = MIMEMultipart()
            msg["Subject"] = title
            msg["From"] = f"{self.sender_display_name} <{self.sender_email}>"
            msg["To"] = address
            msg.attach(MIMEText(body, "plain"))

            for path in (attachments or []):
                path = Path(path)
                mime_type, _ = mimetypes.guess_type(str(path))
                if mime_type and "/" in mime_type:
                    maintype, subtype = mime_type.split("/", 1)
                else:
                    maintype, subtype = "application", "octet-stream"

                file_bytes = path.read_bytes()
                if maintype == "application":
                    part = MIMEApplication(file_bytes, Name=path.name)
                else:
                    part = MIMEBase(maintype, subtype)
                    part.set_payload(file_bytes)
                    from email import encoders
                    encoders.encode_base64(part)
                part["Content-Disposition"] = f'attachment; filename="{path.name}"'
                msg.attach(part)

            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")

            service = self._get_service()
            result = (
                service.users()
                .messages()
                .send(userId="me", body={"raw": raw})
                .execute()
            )

            return ChannelResult(
                delivered=True,
                channel=self.name,
                metadata={"message_id": result.get("id")},
            )
        except Exception as e:
            logger.error("gmail_dwd send failure: %s", e)
            return ChannelResult(
                delivered=False,
                channel=self.name,
                error=f"{type(e).__name__}: {e}",
            )


default_registry.register("gmail_dwd", GmailDwdChannel)
