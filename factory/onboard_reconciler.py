"""Onboarding reconciler — applies completed invitations.

Polls devbrain.invitations for rows with status='ready' (i.e., both
pubkey + oauth-token submitted via the webhook). For each:

  1. Validate the pubkey shape one more time (catches bad data the
     webhook accepted via a more lenient regex).
  2. Append the pubkey to ~lhtdev/.ssh/authorized_keys with a
     traceable comment marker (`# devbrain:<dev_id>:<invite_id>`).
  3. Provision the per-dev profile dir (mkdirs + .gitconfig).
  4. Stash the OAuth token at <profile>/.claude/oauth-token (mode 600).
  5. NULL out the oauth_token column in the DB so it's no longer
     resident in Postgres.
  6. Mark the invitation status='activated', set activated_at.
  7. Fire a notification to the admin (using existing channels).

If `auto_activate=False` on an invitation, skip step 2-6 and emit a
notification telling the admin to run `devbrain setup activate --dev
<id>` manually.

Runs as a periodic poller (default 30s) under launchd or as a
one-shot via `--once` for testing. Uses an advisory lock so two
reconcilers running concurrently can't apply the same invitation
twice.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("devbrain.reconciler")

# Marker comment we attach to authorized_keys entries. Lets us find +
# remove a dev's key cleanly on offboarding (devbrain logout, future
# `devbrain setup remove-dev`). Format: `# devbrain:<dev_id>:<invite_id_short>`
_AUTHORIZED_KEY_MARKER_PREFIX = "# devbrain:"


def reconcile_once(db, *, default_authorized_keys: Optional[Path] = None) -> int:
    """Process every ready invitation. Returns count of activations.

    Idempotent — running twice is safe; rows in 'activated' state are
    skipped. We use a per-row advisory lock (Postgres pg_try_advisory_xact_lock)
    so concurrent reconcilers can't double-apply a single invitation.
    """
    from invitations import list_invitations, expire_overdue
    expired = expire_overdue(db)
    if expired:
        logger.info("Expired %d overdue invitation(s)", expired)

    count = 0
    for inv in list_invitations(db, status="ready"):
        if _try_activate(db, inv, default_authorized_keys=default_authorized_keys):
            count += 1
    return count


def _try_activate(
    db,
    invitation,
    *,
    default_authorized_keys: Optional[Path] = None,
) -> bool:
    """Activate a single invitation. Returns True if activated."""
    # Defensive re-fetch under advisory lock to avoid TOCTOU.
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT pg_try_advisory_xact_lock(hashtext(%s::text))
            """,
            (invitation.id,),
        )
        got_lock = cur.fetchone()[0]
        if not got_lock:
            logger.debug("invitation %s already being processed", invitation.id[:8])
            return False
        # Re-read the row inside the txn — its state may have changed
        # between the listing and the lock acquisition.
        cur.execute(
            """
            SELECT id, dev_id, pubkey, oauth_token, status, auto_activate,
                   email, notes, COALESCE(cli, 'claude') AS cli
            FROM devbrain.invitations
            WHERE id = %s
            FOR UPDATE
            """,
            (invitation.id,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        (id_, dev_id, pubkey, oauth_token, status, auto_activate,
         email, notes, cli) = row
        if status != "ready":
            return False
        if not auto_activate:
            logger.info(
                "invitation %s for dev=%s ready but auto_activate=false; "
                "admin must run `devbrain setup activate --dev %s`",
                str(id_)[:8], dev_id, dev_id,
            )
            # Don't actually activate. Just notify.
            _notify_admin(
                db, dev_id, status="ready_pending_manual",
                detail=(
                    f"Invitation for {dev_id} is ready (pubkey + token "
                    f"both received). Manual activation required: run "
                    f"`devbrain setup activate --dev {dev_id}`"
                ),
            )
            return False

        # ─── 1. Validate pubkey shape (re-check) ───────────────────
        if not _is_safe_pubkey_line(pubkey):
            logger.error(
                "invitation %s pubkey failed safety check; marking expired",
                str(id_)[:8],
            )
            cur.execute(
                "UPDATE devbrain.invitations SET status='expired' WHERE id=%s",
                (id_,),
            )
            return False

        # ─── 2. Append pubkey to authorized_keys ──────────────────
        ak_path = default_authorized_keys or _resolve_authorized_keys()
        marker = f"{_AUTHORIZED_KEY_MARKER_PREFIX}{dev_id}:{str(id_)[:8]}"
        try:
            _ensure_pubkey_in_authorized_keys(ak_path, pubkey, marker)
        except OSError as e:
            logger.error(
                "invitation %s could not write authorized_keys: %s — leaving as 'ready'",
                str(id_)[:8], e,
            )
            return False

        # ─── 3. Provision profile dir + 4. stash CLI credential ──────
        profile_dir = _resolve_profile_dir(dev_id)
        profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            _stash_credential(profile_dir, oauth_token, cli=cli)
        except OSError as e:
            logger.error(
                "invitation %s could not stash credential (cli=%s): %s — rolling back authorized_keys",
                str(id_)[:8], cli, e,
            )
            _remove_marker_from_authorized_keys(ak_path, marker)
            return False
        # Provision .gitconfig if absent (best-effort; the dev's full
        # name + email live on the dev row).
        _ensure_gitconfig(profile_dir, db, dev_id)

        # ─── 5. Defense-in-depth: ensure no stale bootstrap marker ─────
        # `onboard_rotate.sh` self-deletes its own bootstrap entry from
        # authorized_keys on success. If it died mid-way (process kill,
        # disk full, etc.), the marker may still be present. We sweep
        # it here as a safety net — even though the temp key has an
        # `expiry-time` enforced by sshd, we don't want stale entries
        # accumulating.
        bootstrap_marker = f"# devbrain:bootstrap:{dev_id}:{str(id_)[:8]}"
        try:
            _remove_marker_from_authorized_keys(ak_path, bootstrap_marker)
        except OSError as e:
            logger.warning(
                "could not sweep bootstrap marker for %s: %s (non-fatal)",
                dev_id, e,
            )

        # ─── 6. NULL the oauth_token in DB; 7. mark activated ─────
        cur.execute(
            """
            UPDATE devbrain.invitations
            SET oauth_token = NULL,
                status = 'activated',
                activated_at = NOW()
            WHERE id = %s
            """,
            (id_,),
        )
        conn.commit()

    # ─── 7. Notify admin ──────────────────────────────────────────
    cli_label = cli if cli else "claude"
    _notify_admin(
        db, dev_id, status="activated",
        detail=(
            f"Dev '{dev_id}' is now live (cli={cli_label}). Pubkey added to "
            f"authorized_keys, credential stashed at per-profile path. "
            f"Factory is ready to spawn {cli_label} on their behalf."
        ),
    )

    logger.info("Activated invitation for dev=%s", dev_id)
    return True


# ─── Filesystem helpers ──────────────────────────────────────────────────────

def _resolve_authorized_keys() -> Path:
    """Path to the operator account's authorized_keys file.

    DEVBRAIN_AUTHORIZED_KEYS env var overrides for testing; otherwise
    falls back to the running user's ~/.ssh/authorized_keys.
    """
    override = os.environ.get("DEVBRAIN_AUTHORIZED_KEYS")
    if override:
        return Path(override)
    return Path.home() / ".ssh" / "authorized_keys"


def _resolve_profile_dir(dev_id: str) -> Path:
    """Return <DEVBRAIN_HOME>/profiles/<dev_id>."""
    home = Path(os.environ.get("DEVBRAIN_HOME", str(Path.home() / "devbrain")))
    return home / "profiles" / dev_id


def _is_safe_pubkey_line(s: str) -> bool:
    """Re-validate the pubkey before writing to authorized_keys.

    Refuses anything with a newline (would let an attacker inject
    multiple authorized_keys lines), refuses leading 'command='/'no-pty'
    options that grant unintended capabilities, and confirms a known
    SSH key prefix.
    """
    if not isinstance(s, str):
        return False
    s = s.strip()
    if not s:
        return False
    if "\n" in s or "\r" in s:
        return False
    parts = s.split(None, 2)
    if len(parts) < 2:
        return False
    keytype = parts[0]
    valid_types = {
        "ssh-ed25519", "ssh-rsa", "ssh-dss",
        "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com",
    }
    if keytype not in valid_types:
        return False
    return True


def _ensure_pubkey_in_authorized_keys(path: Path, pubkey: str, marker: str) -> None:
    """Append pubkey + marker comment to authorized_keys (idempotent).

    If a line with the same marker already exists (from a prior failed
    activation that we're retrying), we leave it alone — the entry is
    already there. Writes the file atomically via rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text() if path.exists() else ""
    if marker in existing:
        logger.debug("authorized_keys already contains marker %s", marker)
        return

    # Append a clean entry: marker comment on its own line, then the
    # pubkey on the next line. This makes removal grep-friendly — find
    # the marker, delete that line + the next.
    new_block = f"\n{marker}\n{pubkey.strip()}\n"
    payload = existing.rstrip() + new_block
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload)
    tmp.chmod(0o600)
    tmp.replace(path)


def _remove_marker_from_authorized_keys(path: Path, marker: str) -> None:
    """Best-effort rollback: strip the marker block from authorized_keys."""
    if not path.exists():
        return
    lines = path.read_text().splitlines(keepends=False)
    out: list[str] = []
    skip_next = False
    for line in lines:
        if skip_next:
            skip_next = False
            continue
        if line.strip() == marker:
            skip_next = True
            continue
        out.append(line)
    path.write_text("\n".join(out) + "\n")
    path.chmod(0o600)


def _stash_credential(profile_dir: Path, credential: str, cli: str = "claude") -> None:
    """Stash the CLI credential at the correct per-profile path (mode 600).

    Routing by CLI:
      claude: <profile>/.claude/oauth-token
              Plain text sk-ant-oat01-... value.

      codex:  <profile>/.codex/auth.json
              JSON content of the dev's ~/.codex/auth.json.  The factory
              spawn env sets CODEX_HOME=<profile>/.codex so codex picks
              this up automatically.

      gemini: <profile>/.devbrain/env (KEY=VALUE format, sourced by spawn)
              Appends/overwrites GEMINI_API_KEY=<key>. The spawn env sets
              GEMINI_API_KEY from this file so gemini picks it up.
    """
    if not credential:
        raise ValueError("credential is empty")

    cli = cli or "claude"

    if cli == "claude":
        f = profile_dir / ".claude" / "oauth-token"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(credential.strip())
        f.chmod(0o600)

    elif cli == "codex":
        f = profile_dir / ".codex" / "auth.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(credential.strip())
        f.chmod(0o600)

    elif cli == "gemini":
        env_dir = profile_dir / ".devbrain"
        env_dir.mkdir(parents=True, exist_ok=True)
        env_file = env_dir / "env"
        # Read existing lines, replace or append GEMINI_API_KEY.
        lines: list[str] = []
        if env_file.exists():
            lines = [
                ln for ln in env_file.read_text().splitlines()
                if not ln.startswith("GEMINI_API_KEY=")
            ]
        lines.append(f"GEMINI_API_KEY={credential.strip()}")
        env_file.write_text("\n".join(lines) + "\n")
        env_file.chmod(0o600)

    else:
        raise ValueError(f"Unknown CLI: {cli!r}")


# Keep the old name as an alias for backward compatibility with any
# external callers that might reference it directly.
def _stash_oauth_token(profile_dir: Path, oauth_token: str) -> None:
    """Backward-compat alias — stash a Claude oauth-token."""
    _stash_credential(profile_dir, oauth_token, cli="claude")


def _ensure_gitconfig(profile_dir: Path, db, dev_id: str) -> None:
    """Best-effort .gitconfig populate. Skips if file already exists."""
    gc = profile_dir / ".gitconfig"
    if gc.exists():
        return
    full_name = dev_id
    email = f"{dev_id}@devbrain.local"
    try:
        row = db.get_dev(dev_id) if hasattr(db, "get_dev") else None
        if row:
            full_name = row.get("full_name") or full_name
            for ch in row.get("channels") or []:
                if isinstance(ch, dict) and ch.get("type") == "email":
                    email = ch.get("address") or email
                    break
    except Exception as e:
        logger.debug("get_dev failed during gitconfig provision: %s", e)
    gc.write_text(f"[user]\n\tname = {full_name}\n\temail = {email}\n")


# ─── Notifications ───────────────────────────────────────────────────────────

def _notify_admin(db, dev_id: str, *, status: str, detail: str) -> None:
    """Best-effort admin notification. Failures are logged + ignored."""
    try:
        from notifications.router import NotificationRouter
        from notifications.types import NotificationEvent
        title = (
            f"Onboarding complete: {dev_id}"
            if status == "activated"
            else f"Onboarding ready (manual): {dev_id}"
        )
        router = NotificationRouter(db)
        admin = os.environ.get("DEVBRAIN_ADMIN_DEV_ID") or os.environ.get("USER")
        if not admin:
            logger.debug("no admin dev_id resolvable; skipping notification")
            return
        router.dispatch(
            NotificationEvent(
                event_type="needs_human" if status != "activated" else "job_ready",
                recipient_dev_id=admin,
                title=title,
                body=detail,
            )
        )
    except Exception as e:
        logger.debug("admin notification failed (non-fatal): %s", e)


# ─── Entrypoint ──────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="DevBrain onboarding reconciler — applies ready invitations.",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single reconciliation pass and exit (for testing).",
    )
    parser.add_argument(
        "--interval", type=int, default=30,
        help="Polling interval in seconds (default: 30).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from state_machine import FactoryDB
    from config import DATABASE_URL
    db = FactoryDB(DATABASE_URL)

    if args.once:
        n = reconcile_once(db)
        logger.info("Reconciler one-shot: %d activated", n)
        return 0

    logger.info("Reconciler running, interval=%ds", args.interval)
    while True:
        try:
            n = reconcile_once(db)
            if n:
                logger.info("Reconciler tick: %d activated", n)
        except Exception as e:
            logger.exception("Reconciler tick raised: %s", e)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
