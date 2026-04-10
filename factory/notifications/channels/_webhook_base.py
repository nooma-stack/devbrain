"""Shared HTTP POST helper for webhook channels."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from notifications.base import ChannelResult

logger = logging.getLogger(__name__)


def post_webhook(url: str, payload: dict, channel_name: str, timeout: int = 15) -> ChannelResult:
    """POST JSON payload to webhook URL. Returns a ChannelResult."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            if 200 <= code < 300:
                logger.info("Webhook %s delivered to %s (HTTP %s)", channel_name, url[:50], code)
                return ChannelResult(delivered=True, channel=channel_name)
            return ChannelResult(
                delivered=False,
                channel=channel_name,
                error=f"HTTP {code}",
            )
    except urllib.error.HTTPError as e:
        return ChannelResult(
            delivered=False,
            channel=channel_name,
            error=f"HTTP {e.code}: {e.reason}",
        )
    except Exception as e:
        return ChannelResult(
            delivered=False,
            channel=channel_name,
            error=f"{type(e).__name__}: {e}",
        )
