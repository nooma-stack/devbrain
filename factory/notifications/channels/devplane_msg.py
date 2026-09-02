"""Devplane-messaging notification channel — reaches the human's LIVE SESSIONS.

Posts to devplane's messaging centre (``POST /api/messages/send``) addressed
to a person handle. The per-machine courier daemons then execute the delivery
ladder and the message lands as a visible card in the recipient's active
Claude sessions — their "desk", not just their inbox. This is the channel
that makes an alert impossible to miss for someone who lives in a terminal.

PHI boundary (courier spec §2.1): devplane carries pointers and metadata
only. This channel is for OPERATIONAL text (health checks, job states) —
never route message bodies containing clinical or personal content here.

Config (yaml ``notifications.channels.devplane_msg``), with env fallbacks so
machines that already carry devplane credentials need no duplication:
    url         (or env DEVPLANE_URL)
    token       (or env DEVPLANE_TOKEN)
    agent_code  (or env DEVPLANE_AGENT)
    project     (default "brightbot")
Address = the recipient's devplane handle (e.g. ``patrick``).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request

from notifications.base import ChannelResult, NotificationChannel, default_registry

logger = logging.getLogger(__name__)


class DevplaneMsgChannel(NotificationChannel):
    name = "devplane_msg"

    def __init__(self, url: str = "", token: str = "", agent_code: str = "",
                 project: str = "brightbot", **kwargs):
        super().__init__(url=url, token=token, agent_code=agent_code,
                         project=project, **kwargs)
        self.url = (url or os.environ.get("DEVPLANE_URL", "")).rstrip("/")
        self.token = token or os.environ.get("DEVPLANE_TOKEN", "")
        self.agent_code = agent_code or os.environ.get("DEVPLANE_AGENT", "")
        self.project = project

    def is_configured(self) -> bool:
        return bool(self.url and self.token and self.agent_code)

    def send(self, address: str, title: str, body: str,
             event_type: str = "", **kwargs) -> ChannelResult:
        payload = {
            "project": self.project,
            "agent_code": self.agent_code,
            "to": [address],
            "subject": title,
            "body": f"[{event_type or 'notification'}] {title}\n\n{body}",
            "kind": "message",
        }
        req = urllib.request.Request(
            f"{self.url}/api/messages/send",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read())
            return ChannelResult(
                delivered=True, channel=self.name,
                metadata={"thread_id": resp.get("thread_id")})
        except Exception as exc:  # noqa: BLE001 — report, never raise
            logger.warning("devplane_msg send failed: %s", exc)
            return ChannelResult(delivered=False, channel=self.name,
                                 error=str(exc)[:200])


default_registry.register("devplane_msg", DevplaneMsgChannel)
