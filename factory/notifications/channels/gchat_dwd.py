"""Google Chat notification channel using Domain-Wide Delegation.

Uses a Google Workspace service account with domain-wide delegation to send
Chat messages as a workspace user. Supports auto-creating DM spaces on first
use when given an email address as the target.
"""

from __future__ import annotations

import logging
from pathlib import Path

from notifications.base import ChannelResult, NotificationChannel, default_registry

logger = logging.getLogger(__name__)

# Lazy-check for google libs so the module imports cleanly even if the deps
# are missing. is_configured() will return False in that case.
try:
    from google.oauth2 import service_account  # type: ignore
    from googleapiclient.discovery import build  # type: ignore

    GOOGLE_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when deps missing
    GOOGLE_AVAILABLE = False


SCOPES = [
    "https://www.googleapis.com/auth/chat.messages.create",
    "https://www.googleapis.com/auth/chat.spaces.create",
]


class GchatDwdChannel(NotificationChannel):
    """Send Google Chat messages via a DWD service account."""

    name = "gchat_dwd"

    def __init__(
        self,
        service_account_path: str = "",
        sender_email: str = "",
        auto_create_dm_space: bool = True,
        **kwargs,
    ):
        super().__init__(
            service_account_path=service_account_path,
            sender_email=sender_email,
            auto_create_dm_space=auto_create_dm_space,
            **kwargs,
        )
        self.service_account_path = service_account_path
        self.sender_email = sender_email
        self.auto_create_dm_space = auto_create_dm_space
        # Cache mapping recipient email -> Chat space resource name
        self._email_to_space: dict[str, str] = {}

    def is_configured(self) -> bool:
        if not GOOGLE_AVAILABLE:
            return False
        if not self.service_account_path:
            return False
        if not Path(self.service_account_path).exists():
            return False
        if not self.sender_email:
            return False
        return True

    def _get_service(self):
        """Build a Chat v1 API client using DWD credentials."""
        creds = service_account.Credentials.from_service_account_file(
            self.service_account_path,
            scopes=SCOPES,
        )
        # Impersonate the sender via domain-wide delegation
        delegated = creds.with_subject(self.sender_email)
        return build("chat", "v1", credentials=delegated, cache_discovery=False)

    def _create_dm_space(self, recipient_email: str) -> str | None:
        """Create a direct-message space with the recipient. Returns space name."""
        try:
            service = self._get_service()
            response = (
                service.spaces()
                .setup(
                    body={
                        "space": {"spaceType": "DIRECT_MESSAGE"},
                        "memberships": [
                            {
                                "member": {
                                    "name": f"users/{recipient_email}",
                                    "type": "HUMAN",
                                }
                            }
                        ],
                    }
                )
                .execute()
            )
            return response.get("name")
        except Exception as e:
            logger.error("Failed to create DM space for %s: %s", recipient_email, e)
            return None

    def send(
        self, address: str, title: str, body: str, **kwargs
    ) -> ChannelResult:
        if not self.is_configured():
            return ChannelResult(
                delivered=False,
                channel=self.name,
                error="gchat_dwd channel is not configured",
            )

        try:
            # Resolve the target space resource name
            if address.startswith("spaces/"):
                space_id = address
            elif "@" in address and self.auto_create_dm_space:
                cached = self._email_to_space.get(address)
                if cached:
                    space_id = cached
                else:
                    created = self._create_dm_space(address)
                    if not created:
                        return ChannelResult(
                            delivered=False,
                            channel=self.name,
                            error=f"Failed to create DM space for {address}",
                        )
                    self._email_to_space[address] = created
                    space_id = created
            else:
                return ChannelResult(
                    delivered=False,
                    channel=self.name,
                    error=f"Invalid address format: {address!r}",
                )

            formatted = f"*🔔 {title}*\n\n{body}"

            service = self._get_service()
            response = (
                service.spaces()
                .messages()
                .create(parent=space_id, body={"text": formatted})
                .execute()
            )

            return ChannelResult(
                delivered=True,
                channel=self.name,
                metadata={
                    "space_id": space_id,
                    "message_id": response.get("name"),
                },
            )
        except Exception as e:
            logger.error("gchat_dwd send failure: %s", e)
            return ChannelResult(
                delivered=False,
                channel=self.name,
                error=f"{type(e).__name__}: {e}",
            )


default_registry.register("gchat_dwd", GchatDwdChannel)
