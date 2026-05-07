"""Helper invoked by onboard_rotate.sh — persist pubkey rotation to DB.

Receives JSON on stdin:
  {"pubkey": "ssh-ed25519 AAAA...", "cli": "claude"|"codex"|"gemini"}

Receives invite_id_short via argv.

Looks up the invitation row by id prefix, sets pubkey + pubkey_received_at,
flips status pending → ready so the reconciler picks it up. The CLI's
auth credential (oauth-token / codex auth.json / gemini api-key) is
NOT collected here — the dev runs `devbrain login` server-side after
the reconciler has appended their pubkey to authorized_keys, and the
adapter writes the credential directly into the dev's profile dir.
"""

from __future__ import annotations

import argparse
import json
import os
import sys


_VALID_CLIS = ("claude", "codex", "gemini")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--invite-id-short", required=True,
                        help="First 8 chars of the invitation UUID")
    args = parser.parse_args()

    try:
        body = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"invalid_json: {e}", file=sys.stderr)
        return 2

    pubkey = body.get("pubkey", "").strip()
    cli = body.get("cli", "claude").strip()

    if not pubkey:
        print("missing_pubkey", file=sys.stderr)
        return 2

    if cli not in _VALID_CLIS:
        print(f"unknown_cli: {cli}", file=sys.stderr)
        return 2

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from onboard_reconciler import _is_safe_pubkey_line
    if not _is_safe_pubkey_line(pubkey):
        print("pubkey_unsafe", file=sys.stderr)
        return 2

    from state_machine import FactoryDB
    from config import DATABASE_URL
    db = FactoryDB(DATABASE_URL)

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE devbrain.invitations
            SET pubkey = %s,
                pubkey_received_at = COALESCE(pubkey_received_at, NOW()),
                status = CASE
                    WHEN status IN ('pending','ready') THEN 'ready'
                    ELSE status
                END
            WHERE id::text LIKE %s
              AND status IN ('pending', 'ready')
              AND expires_at > NOW()
            RETURNING id, dev_id, status
            """,
            (pubkey, f"{args.invite_id_short}%"),
        )
        row = cur.fetchone()
        conn.commit()

    if row is None:
        print(f"no_matching_invitation_for_prefix={args.invite_id_short}", file=sys.stderr)
        return 1

    inv_id, dev_id, status = row
    print(f"rotated dev={dev_id} invite={str(inv_id)[:8]} cli={cli} status={status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
