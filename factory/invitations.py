"""Onboarding invitation lifecycle.

An invitation is a single-use, time-bounded credential that lets a new
dev (or their agent) submit a pubkey + OAuth token to DevBrain over an
internet-exposed webhook. The flow:

  1. admin runs `devbrain setup add-dev` → creates devs row + invitation
     row + onboarding kit `.md` file. We return the RAW token to the
     caller exactly once; only its SHA-256 hash lives in the DB.
  2. admin sends the .md to the dev (email, Slack, hand-off, …).
  3. the dev (or their agent) POSTs pubkey to
     /onboard/<token>/pubkey on the webhook service. Webhook hashes,
     looks up, stores pubkey + timestamp.
  4. dev runs `claude setup-token` on their laptop, POSTs the resulting
     `sk-ant-oat01-...` to /onboard/<token>/oauth-token.
  5. when both pubkey and oauth-token are received, status flips to
     `ready`. Reconciler picks it up, writes pubkey to
     ~lhtdev/.ssh/authorized_keys, stashes oauth-token at
     <profile>/.claude/oauth-token, NULLs the token in the DB row, marks
     status=`activated`. Notification fires to the admin.

Token shape: `dvbn_inv_<32 base32 chars>`. Prefixed for human
recognizability and so we can distinguish at a glance from API keys or
OAuth tokens. Random component is 160 bits of entropy (32 base32 chars
= 32 * 5 = 160 bits); collision probability is negligible at our scale.

Hashing: SHA-256, hex-encoded. Cryptographically inappropriate for
high-value secrets (no salt, fast hash) but adequate for this use case
because tokens have 7-day TTLs, are single-use, and rotate per dev.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# 32 base32 chars = 160 bits of entropy. Worth being precise: secrets'
# default `token_urlsafe(20)` gives 160 bits via base64url; we want
# base32 specifically so the token is uppercase-friendly and easy to
# read aloud / paste without ambiguity (no 0/O confusion since base32
# uses A-Z + 2-7).
_TOKEN_PREFIX = "dvbn_inv_"
_TOKEN_BYTES = 20  # 160 bits


def generate_token() -> str:
    """Return a new raw invitation token. Caller owns it; we hash + forget."""
    raw = secrets.token_hex(_TOKEN_BYTES).upper()
    return f"{_TOKEN_PREFIX}{raw}"


def hash_token(raw_token: str) -> str:
    """Return the hex-encoded SHA-256 digest used as the DB lookup key."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


@dataclass
class Invitation:
    """One row from devbrain.invitations."""
    id: str
    dev_id: str
    pubkey: Optional[str]
    pubkey_received_at: Optional[datetime]
    oauth_token: Optional[str]
    oauth_token_received_at: Optional[datetime]
    status: str
    auto_activate: bool
    email: Optional[str]
    notes: Optional[str]
    created_by: Optional[str]
    created_at: datetime
    expires_at: datetime
    activated_at: Optional[datetime]

    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at

    @property
    def is_ready(self) -> bool:
        """Both pubkey and oauth-token have been submitted."""
        return self.pubkey is not None and self.oauth_token is not None


def create_invitation(
    db,
    *,
    dev_id: str,
    auto_activate: bool = True,
    email: Optional[str] = None,
    notes: Optional[str] = None,
    created_by: Optional[str] = None,
    ttl_days: int = 7,
    cli: str = "claude",
) -> tuple[Invitation, str]:
    """Stage a new invitation. Returns (Invitation, raw_token).

    The raw_token is returned ONCE to the caller and never persisted —
    after this call, the only way to identify the invitation is via the
    hash. Caller should embed the raw token in the onboarding kit and
    show it to no one else.

    Args:
        cli: AI CLI the invitation is for ('claude', 'codex', 'gemini').
             Stored in the cli column (migration 031) so the reconciler
             knows which credential stash path to use. Defaults to 'claude'
             for backward compatibility with callers that don't pass cli.
    """
    raw_token = generate_token()
    token_hash = hash_token(raw_token)
    expires_at = datetime.now(timezone.utc) + timedelta(days=ttl_days)

    # Use COALESCE-safe INSERT — if the migration hasn't run yet on an
    # older instance, the INSERT will fail on the unknown column. Callers
    # on pre-031 instances should pass cli=None to skip the column.
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO devbrain.invitations
                (dev_id, token_hash, status, auto_activate,
                 email, notes, created_by, expires_at, cli)
            VALUES (%s, %s, 'pending', %s, %s, %s, %s, %s, %s)
            RETURNING id, dev_id, pubkey, pubkey_received_at,
                      oauth_token, oauth_token_received_at, status,
                      auto_activate, email, notes, created_by,
                      created_at, expires_at, activated_at
            """,
            (dev_id, token_hash, auto_activate, email, notes, created_by, expires_at, cli),
        )
        row = cur.fetchone()
        conn.commit()

    inv = _row_to_invitation(row, cur.description)
    logger.info(
        "Created invitation %s for dev %s (cli=%s, expires %s)",
        inv.id[:8], dev_id, cli, expires_at,
    )
    return inv, raw_token


def get_invitation_by_token(db, raw_token: str) -> Optional[Invitation]:
    """Look up an invitation by raw token. Returns None if not found."""
    token_hash = hash_token(raw_token)
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, dev_id, pubkey, pubkey_received_at,
                   oauth_token, oauth_token_received_at, status,
                   auto_activate, email, notes, created_by,
                   created_at, expires_at, activated_at
            FROM devbrain.invitations
            WHERE token_hash = %s
            """,
            (token_hash,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_invitation(row, cur.description)


def submit_pubkey(db, raw_token: str, pubkey: str) -> Optional[Invitation]:
    """Webhook handler: store the dev's pubkey on the invitation.

    Returns the updated Invitation, or None if the token is invalid /
    expired / already-submitted. Caller (the webhook) maps None →
    HTTP 404/410 as appropriate.

    Validates pubkey shape minimally — must look like an SSH pubkey
    (ssh-ed25519 / ssh-rsa / ecdsa-sha2-* prefix). Heavier validation
    happens in the reconciler before write to authorized_keys.
    """
    pubkey = pubkey.strip()
    if not _looks_like_ssh_pubkey(pubkey):
        logger.warning("Rejected pubkey submission for token %s: malformed", raw_token[:16])
        return None

    return _update_invitation_field(
        db, raw_token, "pubkey", pubkey, "pubkey_received_at",
    )


def submit_oauth_token(db, raw_token: str, oauth_token: str) -> Optional[Invitation]:
    """Webhook handler: store the dev's claude OAuth token.

    Validates the token shape. The actual stash to file happens later
    in the reconciler.
    """
    oauth_token = oauth_token.strip()
    if not _looks_like_oauth_token(oauth_token):
        logger.warning("Rejected oauth-token submission for token %s: malformed", raw_token[:16])
        return None

    return _update_invitation_field(
        db, raw_token, "oauth_token", oauth_token, "oauth_token_received_at",
    )


def list_invitations(db, status: Optional[str] = None) -> list[Invitation]:
    """List invitations, newest first. Used by `devbrain setup invitations`."""
    sql = """
        SELECT id, dev_id, pubkey, pubkey_received_at,
               oauth_token, oauth_token_received_at, status,
               auto_activate, email, notes, created_by,
               created_at, expires_at, activated_at
        FROM devbrain.invitations
    """
    params: list = []
    if status:
        sql += " WHERE status = %s"
        params.append(status)
    sql += " ORDER BY created_at DESC"

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        descs = cur.description
    return [_row_to_invitation(r, descs) for r in rows]


def revoke_invitation(db, invitation_id: str) -> bool:
    """Mark an invitation as revoked (admin action)."""
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE devbrain.invitations
            SET status = 'revoked'
            WHERE id::text LIKE %s AND status NOT IN ('activated', 'revoked')
            RETURNING id
            """,
            (f"{invitation_id}%",),
        )
        row = cur.fetchone()
        conn.commit()
    return row is not None


def expire_overdue(db) -> int:
    """Bulk-mark invitations whose expires_at has passed. Returns count."""
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE devbrain.invitations
            SET status = 'expired'
            WHERE expires_at < NOW() AND status IN ('pending', 'ready')
            """,
        )
        count = cur.rowcount
        conn.commit()
    if count:
        logger.info("Expired %d overdue invitation(s)", count)
    return count


# ─── Internals ─────────────────────────────────────────────────────────────

def _row_to_invitation(row, description) -> Invitation:
    columns = [d.name for d in description]
    data = dict(zip(columns, row))
    return Invitation(
        id=str(data["id"]),
        dev_id=data["dev_id"],
        pubkey=data["pubkey"],
        pubkey_received_at=data["pubkey_received_at"],
        oauth_token=data["oauth_token"],
        oauth_token_received_at=data["oauth_token_received_at"],
        status=data["status"],
        auto_activate=data["auto_activate"],
        email=data["email"],
        notes=data["notes"],
        created_by=data["created_by"],
        created_at=data["created_at"],
        expires_at=data["expires_at"],
        activated_at=data["activated_at"],
    )


def _update_invitation_field(
    db,
    raw_token: str,
    value_column: str,
    value: str,
    timestamp_column: str,
) -> Optional[Invitation]:
    """Atomically set a value column + its received_at timestamp.

    Refuses the update if the invitation is expired, revoked, or already
    activated. Refuses overwriting an already-set value (single-shot).

    Also flips status to `ready` when both pubkey and oauth_token are
    populated post-update.
    """
    token_hash = hash_token(raw_token)
    sql = f"""
        UPDATE devbrain.invitations
        SET {value_column} = %s,
            {timestamp_column} = NOW(),
            status = CASE
                -- both fields present → ready
                WHEN status = 'pending'
                     AND ({_other_field(value_column)} IS NOT NULL OR %s IS NOT NULL)
                     AND %s IS NOT NULL
                THEN 'ready'
                ELSE status
            END
        WHERE token_hash = %s
          AND status IN ('pending', 'ready')
          AND expires_at > NOW()
          AND {value_column} IS NULL
        RETURNING id, dev_id, pubkey, pubkey_received_at,
                  oauth_token, oauth_token_received_at, status,
                  auto_activate, email, notes, created_by,
                  created_at, expires_at, activated_at
    """
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(sql, (value, value, value, token_hash))
        row = cur.fetchone()
        conn.commit()
        if row is None:
            return None
        return _row_to_invitation(row, cur.description)


def _other_field(field: str) -> str:
    return "oauth_token" if field == "pubkey" else "pubkey"


def _looks_like_ssh_pubkey(s: str) -> bool:
    parts = s.split()
    if len(parts) < 2:
        return False
    return parts[0] in (
        "ssh-ed25519", "ssh-rsa", "ssh-dss",
        "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com",
    )


def _looks_like_oauth_token(s: str) -> bool:
    # Anthropic's real prefix is `sk-ant-oatN-` (observed `sk-ant-oat1-`
    # against claude 2.1.132; their docs sometimes show `sk-ant-oat01-`).
    # `sk-ant-` covers both shapes plus any future variants in the same
    # family without forcing this validator to chase format updates.
    return s.startswith("sk-ant-")


def callback_base_url(invite_id: str, raw_token: str) -> str:
    """The webhook base URL the dev's agent POSTs to.

    Hard-coded to the LHT VPS Traefik for now. Could be made configurable
    via factory.config later if other DevBrain installations spin up.
    """
    return f"https://devbrain.lighthouse-therapy.com/onboard/{raw_token}"
