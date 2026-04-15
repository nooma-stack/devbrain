"""Subprocess entrypoint for sending notifications from the MCP server.

Called as: python notify_cli.py <recipient_dev_id> <event_type> <title_file> <body_file>
Reads title and body from files to avoid shell escaping issues.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add factory dir to path
sys.path.insert(0, str(Path(__file__).parent))

from config import DATABASE_URL
from state_machine import FactoryDB
from notifications.router import NotificationRouter, NotificationEvent


def main():
    if len(sys.argv) < 5:
        print("Usage: notify_cli.py <recipient> <event_type> <title_file> <body_file>", file=sys.stderr)
        sys.exit(1)

    recipient = sys.argv[1]
    event_type = sys.argv[2]
    title = Path(sys.argv[3]).read_text()
    body = Path(sys.argv[4]).read_text()

    db = FactoryDB(DATABASE_URL)
    router = NotificationRouter(db)
    result = router.send(NotificationEvent(
        event_type=event_type,
        recipient_dev_id=recipient,
        title=title,
        body=body,
    ))

    if result.skipped:
        print("skipped")
    else:
        print(f"attempted={','.join(result.channels_attempted)}")
        print(f"delivered={','.join(result.channels_delivered)}")


if __name__ == "__main__":
    main()
