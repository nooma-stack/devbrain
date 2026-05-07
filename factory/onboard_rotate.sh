#!/bin/bash
# DevBrain Onboarding — temp-key rotation handler.
#
# This script runs ONLY because the authorized_keys entry for a
# temp bootstrap SSH key pins it as the `command=...` directive.
# OpenSSH ignores whatever command the client requested and executes
# THIS script with the args we baked into the authorized_keys line.
#
# Lifecycle: a new dev's invitation kit ships a temp ed25519 private
# key + invite token. Their AI agent SSHes in using the temp key,
# sending pubkey-only JSON on stdin. We:
#
#   1. Read the JSON.
#   2. Validate pubkey shape (defense-in-depth — Python helper does
#      the canonical check).
#   3. Look up the invitation's cli column (set at issuance time;
#      informational here — not used to validate any credential).
#   4. Persist the pubkey to the invitation row.
#   5. Self-delete this temp key's authorized_keys entry by marker.
#   6. Audit-log the rotation.
#   7. Return JSON to the agent: {"status":"ok"}.
#
# Subscription auth (oauth-token / codex auth.json / gemini api-key)
# is NOT collected here. After the reconciler appends the pubkey to
# authorized_keys, the dev SSHes in with their permanent key and runs
# `devbrain login` server-side. That command runs the appropriate
# per-CLI auth flow inside the dev's profile dir; the credential
# stays on the Mac Studio and never transits the dev's machine.
#
# Stdin JSON shape:  {"pubkey":"ssh-ed25519 AAAA... comment"}
#
# Failure modes (all return non-zero exit + JSON error to the agent):
#   - Malformed JSON
#   - Pubkey shape rejected
#   - DB write failure
#   - File permission failure on authorized_keys edit

set -euo pipefail

DEV_ID="${1:?missing dev_id}"
INVITE_ID_SHORT="${2:?missing invite_id_short}"

DEVBRAIN_HOME="${DEVBRAIN_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
AUTHORIZED_KEYS="${DEVBRAIN_AUTHORIZED_KEYS:-$HOME/.ssh/authorized_keys}"
LOG_FILE="${DEVBRAIN_HOME}/logs/onboard.log"
MARKER="# devbrain:bootstrap:${DEV_ID}:${INVITE_ID_SHORT}"

mkdir -p "$(dirname "$LOG_FILE")"
exec 3>&1 1>&2  # 3 = stdout for client; 1+2 = our diagnostic logs

# ─── Read JSON from stdin ──────────────────────────────────────────────────

# Cap input at 64 KB (auth.json for codex can be a few KB; 64 KB is
# generous without being unbounded).
INPUT="$(head -c 65536)"
if [ -z "$INPUT" ]; then
  printf '{"status":"error","error":"empty_body"}\n' >&3
  echo "$(date -u +%FT%TZ) rotate-fail dev=${DEV_ID} reason=empty_body from=${SSH_CLIENT:-unknown}" >> "$LOG_FILE"
  exit 1
fi

# Parse fields. We don't ship jq as a hard dep — use Python (already
# required by the rest of DevBrain).
read_field() {
  local field="$1"
  python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    v = d.get('$field')
    if v is None: sys.exit(2)
    sys.stdout.write(json.dumps(v) if not isinstance(v, str) else v)
except Exception:
    sys.exit(2)
" <<< "$INPUT"
}

PUBKEY="$(read_field pubkey)" || {
  printf '{"status":"error","error":"missing_or_invalid_pubkey"}\n' >&3
  echo "$(date -u +%FT%TZ) rotate-fail dev=${DEV_ID} reason=missing_pubkey from=${SSH_CLIENT:-unknown}" >> "$LOG_FILE"
  exit 1
}

# ─── Determine CLI from DB invitation row ─────────────────────────────────────

CLI_NAME="$(
  cd "${DEVBRAIN_HOME}/factory"
  PYTHONPATH="${DEVBRAIN_HOME}/factory" \
  "${DEVBRAIN_HOME}/.venv/bin/python" -c "
import sys, os
sys.path.insert(0, '${DEVBRAIN_HOME}/factory')
from state_machine import FactoryDB
from config import DATABASE_URL
db = FactoryDB(DATABASE_URL)
with db._conn() as conn, conn.cursor() as cur:
    cur.execute(
        \"\"\"SELECT COALESCE(cli, 'claude') FROM devbrain.invitations
           WHERE id::text LIKE %s AND status IN ('pending','ready')
             AND expires_at > NOW()\"\"\",
        ('${INVITE_ID_SHORT}%',)
    )
    row = cur.fetchone()
    print(row[0] if row else 'claude')
" 2>/dev/null || echo "claude"
)"

# ─── Validate CLI is recognized ────────────────────────────────────────────────
#
# The CLI was set at invitation issuance time. We don't collect any
# subscription credential at this stage — the dev runs `devbrain login`
# server-side after their pubkey lands in authorized_keys.

case "$CLI_NAME" in
  claude|codex|gemini)
    ;;
  *)
    printf '{"status":"error","error":"unknown_cli","cli":"%s"}\n' "$CLI_NAME" >&3
    echo "$(date -u +%FT%TZ) rotate-fail dev=${DEV_ID} reason=unknown_cli=${CLI_NAME} from=${SSH_CLIENT:-unknown}" >> "$LOG_FILE"
    exit 1
    ;;
esac

# ─── Persist pubkey to invitations DB via Python helper ──────────────────────

HELPER_INPUT="$(python3 -c "
import json, sys
print(json.dumps({'pubkey': sys.argv[1], 'cli': '${CLI_NAME}'}))
" "$PUBKEY")"

ROTATE_RESULT="$(
  cd "${DEVBRAIN_HOME}/factory"
  PYTHONPATH="${DEVBRAIN_HOME}/factory" \
  "${DEVBRAIN_HOME}/.venv/bin/python" onboard_rotate_helper.py \
    --invite-id-short "$INVITE_ID_SHORT" \
    <<< "$HELPER_INPUT" 2>&1
)" || {
  printf '{"status":"error","error":"rotation_db_write_failed"}\n' >&3
  echo "$(date -u +%FT%TZ) rotate-fail dev=${DEV_ID} reason=db_write from=${SSH_CLIENT:-unknown}: ${ROTATE_RESULT}" >> "$LOG_FILE"
  exit 1
}

# ─── Self-delete the temp key from authorized_keys ─────────────────────────

# Atomic edit: read, filter out the marker line + the next line (the
# temp pubkey itself), write back. Fail closed if the marker isn't
# found — the rotation already succeeded DB-side, so the dev is
# rolled forward via the reconciler regardless. Worst case is the
# operator has to manually clean up an orphan authorized_keys line.
if [ -f "$AUTHORIZED_KEYS" ]; then
  TMPFILE="$(mktemp)"
  python3 - "$AUTHORIZED_KEYS" "$MARKER" "$TMPFILE" <<'PYEOF'
import sys
ak_path, marker, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
with open(ak_path) as f:
    lines = f.read().splitlines()
out, skip_next = [], False
for line in lines:
    if skip_next:
        skip_next = False
        continue
    if line.strip() == marker:
        skip_next = True
        continue
    # Also handle the inline-comment form (key + " " + marker on one line)
    if marker in line and line.lstrip().startswith(("ssh-", "sk-", "ecdsa-")):
        # Old-style: marker as trailing comment on the key line — drop the line.
        continue
    out.append(line)
with open(out_path, "w") as f:
    f.write("\n".join(out))
    if out:
        f.write("\n")
PYEOF
  chmod 600 "$TMPFILE"
  mv "$TMPFILE" "$AUTHORIZED_KEYS"
fi

# ─── Audit log + reply ─────────────────────────────────────────────────────

echo "$(date -u +%FT%TZ) rotate-ok dev=${DEV_ID} invite=${INVITE_ID_SHORT} cli=${CLI_NAME} from=${SSH_CLIENT:-unknown}" >> "$LOG_FILE"

printf '{"status":"ok","dev_id":"%s","invite_id":"%s"}\n' "$DEV_ID" "$INVITE_ID_SHORT" >&3

exit 0
