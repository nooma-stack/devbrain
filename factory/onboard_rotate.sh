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
# sending JSON on stdin: {"pubkey": "<their permanent ed25519 pubkey>",
# "oauth_token": "sk-ant-oat01-..."}. We:
#
#   1. Read the JSON.
#   2. Validate pubkey shape (defense-in-depth — Python helper does
#      the canonical check).
#   3. Validate the OAuth token by hitting api.anthropic.com — must
#      return 200 (or 400 for our deliberately-malformed request body
#      against a valid auth header). 401 == invalid token, reject.
#   4. Persist via the same DB path the webhook uses (submit_pubkey,
#      submit_oauth_token) so the reconciler picks it up identically.
#   5. Self-delete this temp key's authorized_keys entry by marker
#      comment.
#   6. Audit-log the rotation (timestamp, dev_id, invite_id_short,
#      SSH client IP).
#   7. Return JSON to the agent: {"status":"ok"}.
#
# Failure modes (all return non-zero exit + JSON error to the agent):
#   - Malformed JSON
#   - Pubkey shape rejected
#   - OAuth token rejected by Anthropic API
#   - DB write failure
#   - File permission failure on authorized_keys edit
#
# Two-factor security: the temp SSH key gets the agent INTO this
# script. The OAuth token (which Alice generated on her laptop via
# `claude setup-token`, requiring her browser auth to claude.com) is
# the SECOND factor that gets the rotation accepted. An attacker who
# intercepts the email gets the temp SSH key + invite token, but can't
# forge a valid sk-ant-oat01-... token — Anthropic only issues those to
# authenticated accounts. Bar to a successful attack: compromise both
# the email channel AND the dev's claude.com account, both within the
# 3-day TTL.

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

# Cap input at 8 KB. Real bodies are <1 KB (pubkey ~80 bytes, token ~120).
INPUT="$(head -c 8192)"
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
    if not isinstance(v, str): sys.exit(2)
    sys.stdout.write(v)
except Exception:
    sys.exit(2)
" <<< "$INPUT"
}

PUBKEY="$(read_field pubkey)" || {
  printf '{"status":"error","error":"missing_or_invalid_pubkey"}\n' >&3
  echo "$(date -u +%FT%TZ) rotate-fail dev=${DEV_ID} reason=missing_pubkey from=${SSH_CLIENT:-unknown}" >> "$LOG_FILE"
  exit 1
}
OAUTH_TOKEN="$(read_field oauth_token)" || {
  printf '{"status":"error","error":"missing_or_invalid_oauth_token"}\n' >&3
  echo "$(date -u +%FT%TZ) rotate-fail dev=${DEV_ID} reason=missing_token from=${SSH_CLIENT:-unknown}" >> "$LOG_FILE"
  exit 1
}

# ─── Validate OAuth token against Anthropic API ────────────────────────────

# A successful `Authorization: Bearer <token>` returns 200 (or 400 with our
# deliberately-minimal body — 400 is fine, it means auth passed and only
# the body shape was wrong). 401 means invalid auth. We treat anything
# other than 200/400 as transient and retry-friendly... but for safety,
# only ACCEPT 200/400. Anything else: reject this attempt.

VALIDATION_HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
  -H "Authorization: Bearer ${OAUTH_TOKEN}" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -X POST https://api.anthropic.com/v1/messages \
  -d '{"model":"claude-haiku-4-5-20251001","max_tokens":1,"messages":[{"role":"user","content":"x"}]}' || echo 000)

case "$VALIDATION_HTTP" in
  200|400) ;;
  401|403)
    printf '{"status":"error","error":"oauth_token_rejected_by_anthropic","http":%s}\n' "$VALIDATION_HTTP" >&3
    echo "$(date -u +%FT%TZ) rotate-fail dev=${DEV_ID} reason=oauth_${VALIDATION_HTTP} from=${SSH_CLIENT:-unknown}" >> "$LOG_FILE"
    exit 1
    ;;
  *)
    printf '{"status":"error","error":"oauth_validation_unreachable","http":%s}\n' "$VALIDATION_HTTP" >&3
    echo "$(date -u +%FT%TZ) rotate-fail dev=${DEV_ID} reason=oauth_${VALIDATION_HTTP} from=${SSH_CLIENT:-unknown}" >> "$LOG_FILE"
    exit 1
    ;;
esac

# ─── Persist to invitations DB via Python helper ───────────────────────────

# Helper handles: pubkey shape validation, submit_pubkey,
# submit_oauth_token, all with the proper hashing/state-machine
# guards. We pass invite_id_short as argv (non-secret) and the
# pubkey + token as JSON on stdin (avoids them appearing in `ps`).
# Construct JSON for the helper without putting the OAuth token in argv
# (argv is visible via `ps` to the same user; stdin isn't).
HELPER_INPUT="$(printf '%s\n---SEP---\n%s' "$PUBKEY" "$OAUTH_TOKEN" | python3 -c '
import json, sys
text = sys.stdin.read()
parts = text.split("\n---SEP---\n", 1)
print(json.dumps({"pubkey": parts[0], "oauth_token": parts[1] if len(parts) > 1 else ""}))
')"

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

echo "$(date -u +%FT%TZ) rotate-ok dev=${DEV_ID} invite=${INVITE_ID_SHORT} from=${SSH_CLIENT:-unknown}" >> "$LOG_FILE"

printf '{"status":"ok","dev_id":"%s","invite_id":"%s"}\n' "$DEV_ID" "$INVITE_ID_SHORT" >&3

exit 0
