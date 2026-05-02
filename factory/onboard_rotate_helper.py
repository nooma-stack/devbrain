"""Helper invoked by onboard_rotate.sh — persist rotation to DB.

Receives JSON on stdin: {"pubkey": "...", "oauth_token": "..."}
Receives invite_id_short via argv.

Looks up the invitation row by id prefix, updates pubkey + oauth_token
columns directly (we already have the invite_id; we don't need the
raw token to find the row, unlike the webhook which is keyed by token
hash). Status flips from pending → ready, then the reconciler does
the rest of the activation work in its next tick.

Why not reuse `submit_pubkey` / `submit_oauth_token` from
invitations.py: those functions key on `token_hash` because the
webhook only has the raw token. Here we already have invite_id_short
(authorized_keys baked it into the command="..." directive), so a
direct UPDATE is more precise and avoids needing to round-trip the
raw token through the rotate.sh script.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--invite-id-short", required=True,
                        help="First 8 chars of the invitation UUID")
    args = parser.parse_args()

    # Read JSON from stdin
    try:
        body = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"invalid_json: {e}", file=sys.stderr)
        return 2

    pubkey = body.get("pubkey", "").strip()
    oauth_token = body.get("oauth_token", "").strip()
    if not pubkey or not oauth_token:
        print("missing_pubkey_or_token", file=sys.stderr)
        return 2

    # Validate pubkey shape using the reconciler's same logic
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from onboard_reconciler import _is_safe_pubkey_line
    if not _is_safe_pubkey_line(pubkey):
        print("pubkey_unsafe", file=sys.stderr)
        return 2

    # OAuth token format check (Anthropic API validation already
    # happened in rotate.sh; this is just a belt-and-suspenders shape
    # check before we persist).
    if not (oauth_token.startswith("sk-ant-oat01-") or oauth_token.startswith("sk-ant-")):
        print("oauth_token_format_invalid", file=sys.stderr)
        return 2

    from state_machine import FactoryDB
    from config import DATABASE_URL
    db = FactoryDB(DATABASE_URL)

    # Direct DB update keyed by invite-id prefix. We require the row
    # to be in pending OR ready state (idempotent retry: if rotate.sh
    # ran once, succeeded the DB write, but failed the authorized_keys
    # cleanup, a retry should still work).
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE devbrain.invitations
            SET pubkey = %s,
                pubkey_received_at = COALESCE(pubkey_received_at, NOW()),
                oauth_token = %s,
                oauth_token_received_at = COALESCE(oauth_token_received_at, NOW()),
                status = CASE
                    WHEN status IN ('pending','ready') THEN 'ready'
                    ELSE status
                END
            WHERE id::text LIKE %s
              AND status IN ('pending', 'ready')
              AND expires_at > NOW()
            RETURNING id, dev_id, status
            """,
            (pubkey, oauth_token, f"{args.invite_id_short}%"),
        )
        row = cur.fetchone()
        conn.commit()

    if row is None:
        print(f"no_matching_invitation_for_prefix={args.invite_id_short}", file=sys.stderr)
        return 1

    inv_id, dev_id, status = row
    print(f"rotated dev={dev_id} invite={str(inv_id)[:8]} status={status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
