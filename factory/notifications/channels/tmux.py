"""Tmux notification channel — non-disruptive popup overlay."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from notifications.base import NotificationChannel, ChannelResult, default_registry

logger = logging.getLogger(__name__)


class TmuxChannel(NotificationChannel):
    name = "tmux"

    def __init__(self, popup_width: int = 70, popup_height: int = 20, **kwargs):
        super().__init__(**kwargs)
        self.popup_width = popup_width
        self.popup_height = popup_height

    def is_configured(self) -> bool:
        return shutil.which("tmux") is not None

    def _is_session_active(self, session_name: str) -> bool:
        try:
            result = subprocess.run(
                ["tmux", "has-session", "-t", session_name],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0
        except (subprocess.SubprocessError, FileNotFoundError):
            return False

    def send(self, address: str, title: str, body: str, **kwargs) -> ChannelResult:
        session_name = address

        if not self._is_session_active(session_name):
            return ChannelResult(
                delivered=False,
                channel=self.name,
                error=f"No active tmux session '{session_name}'",
            )

        tmp_file = Path(tempfile.gettempdir()) / f"devbrain-notif-{uuid.uuid4().hex[:8]}.txt"

        try:
            content = self._format(title, body)
            tmp_file.write_text(content)

            popup_cmd = (
                f"cat {tmp_file} && echo '' && "
                f"echo '[Press any key to dismiss]' && "
                f"read -n 1 && rm {tmp_file}"
            )

            result = subprocess.run(
                [
                    "tmux", "display-popup",
                    "-t", session_name,
                    "-w", str(self.popup_width),
                    "-h", str(self.popup_height),
                    "-E", popup_cmd,
                ],
                capture_output=True, timeout=10,
            )

            if result.returncode != 0:
                return ChannelResult(
                    delivered=False,
                    channel=self.name,
                    error=f"tmux exit {result.returncode}: {result.stderr.decode()[:200]}",
                )

            logger.info("Tmux popup delivered to session '%s'", session_name)
            return ChannelResult(delivered=True, channel=self.name)

        except Exception as e:
            try:
                tmp_file.unlink(missing_ok=True)
            except Exception:
                pass
            return ChannelResult(
                delivered=False,
                channel=self.name,
                error=f"{type(e).__name__}: {e}",
            )

    def _format(self, title: str, body: str) -> str:
        sep = "=" * 60
        return f"🔔  DevBrain Factory\n{sep}\n\n{title}\n\n{sep}\n\n{body}\n"


default_registry.register("tmux", TmuxChannel)
