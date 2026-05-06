"""Helper invoked by onboard_rotate.sh — persist rotation to DB.

Receives JSON on stdin:
  claude: {"pubkey": "...", "cli": "claude", "oauth_token": "sk-ant-..."}
  codex:  {"pubkey": "...", "cli": "codex",  "codex_auth_json": {...}}
  gemini: {"pubkey": "...", "cli": "gemini", "gemini_api_key": "AIza..."}

Receives invite_id_short via argv.

Looks up the invitation row by id prefix, updates the appropriate
credential column and pubkey, then flips status from pending → ready
so the reconciler picks it up.

The reconciler (onboard_reconciler.py) reads the cli column and routes
the credential to the right per-profile stash path:
  claude: <profile>/.claude/oauth-token
  codex:  <profile>/.codex/auth.json
  gemini: <profile>/.devbrain/env (GEMINI_API_KEY=...)

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
    cli = body.get("cli", "claude").strip()

    if not pubkey:
        print("missing_pubkey", file=sys.stderr)
        return 2

    if cli not in ("claude", "codex", "gemini"):
        print(f"unknown_cli: {cli}", file=sys.stderr)
        return 2

    # Validate pubkey shape using the reconciler's same logic
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from onboard_reconciler import _is_safe_pubkey_line
    if not _is_safe_pubkey_line(pubkey):
        print("pubkey_unsafe", file=sys.stderr)
        return 2

    # ─── Extract + validate CLI credential ────────────────────────────────

    if cli == "claude":
        oauth_token = body.get("oauth_token", "").strip()
        if not oauth_token:
            print("missing_oauth_token", file=sys.stderr)
            return 2
        # Belt-and-suspenders format check (Anthropic API validation
        # already happened in rotate.sh).
        if not (oauth_token.startswith("sk-ant-oat01-") or oauth_token.startswith("sk-ant-")):
            print("oauth_token_format_invalid", file=sys.stderr)
            return 2
        # Stored in oauth_token column; reconciler reads it and stashes at
        # <profile>/.claude/oauth-token
        credential_col = "oauth_token"
        credential_val = oauth_token

    elif cli == "codex":
        codex_auth = body.get("codex_auth_json")
        if not codex_auth:
            print("missing_codex_auth_json", file=sys.stderr)
            return 2
        if isinstance(codex_auth, dict):
            credential_val = json.dumps(codex_auth)
        else:
            credential_val = str(codex_auth)
        # Stored in oauth_token column (overloaded for now — all CLIs get
        # one credential slot; the cli column tells the reconciler what
        # it contains). Reconciler writes it to <profile>/.codex/auth.json
        credential_col = "oauth_token"

    elif cli == "gemini":
        gemini_key = body.get("gemini_api_key", "").strip()
        if not gemini_key:
            print("missing_gemini_api_key", file=sys.stderr)
            return 2
        if not gemini_key.startswith("AIza"):
            print("gemini_api_key_format_invalid", file=sys.stderr)
            return 2
        # Stored in oauth_token column; reconciler writes to
        # <profile>/.devbrain/env as GEMINI_API_KEY=...
        credential_col = "oauth_token"
        credential_val = gemini_key

    from state_machine import FactoryDB
    from config import DATABASE_URL
    db = FactoryDB(DATABASE_URL)

    # Direct DB update keyed by invite-id prefix. We require the row
    # to be in pending OR ready state (idempotent retry: if rotate.sh
    # ran once, succeeded the DB write, but failed the authorized_keys
    # cleanup, a retry should still work).
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE devbrain.invitations
            SET pubkey = %s,
                pubkey_received_at = COALESCE(pubkey_received_at, NOW()),
                {credential_col} = %s,
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
            (pubkey, credential_val, f"{args.invite_id_short}%"),
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
