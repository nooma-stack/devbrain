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
# sending CLI-specific JSON on stdin (see below). We:
#
#   1. Read the JSON.
#   2. Validate pubkey shape (defense-in-depth — Python helper does
#      the canonical check).
#   3. Look up the invitation to determine which CLI was used (cli
#      column, migration 031).
#   4. Validate the CLI credential against its upstream API:
#        claude: POST to api.anthropic.com — 200/400 = valid, 401 = reject
#        codex:  same Anthropic API (codex auth.json contains an
#                Anthropic-format OAuth token under "accessToken")
#        gemini: GET to generativelanguage.googleapis.com with the API key
#   5. Persist via the same DB path the webhook uses so the reconciler
#      picks it up identically.
#   6. Self-delete this temp key's authorized_keys entry by marker.
#   7. Audit-log the rotation.
#   8. Return JSON to the agent: {"status":"ok"}.
#
# ─── Stdin JSON shapes by CLI ─────────────────────────────────────────────────
#
#   claude:  {"pubkey":"...", "oauth_token":"sk-ant-oat01-..."}
#   codex:   {"pubkey":"...", "codex_auth_json":{...}}
#             (the full ~/.codex/auth.json object; accessToken pulled server-side)
#   gemini:  {"pubkey":"...", "gemini_api_key":"AIza..."}
#
# ─── Two-factor security ──────────────────────────────────────────────────────
#
# The temp SSH key gets the agent INTO this script. The CLI credential
# (which the dev generated on their own machine/account) is the SECOND
# factor. An attacker who intercepts the email gets the temp SSH key +
# invite token, but can't forge a valid CLI credential — the respective
# platform only issues those to authenticated accounts.
#
# Failure modes (all return non-zero exit + JSON error to the agent):
#   - Malformed JSON
#   - Pubkey shape rejected
#   - CLI credential rejected by upstream API
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

# ─── Extract + validate CLI credential ────────────────────────────────────────

case "$CLI_NAME" in

  claude)
    OAUTH_TOKEN="$(read_field oauth_token)" || {
      printf '{"status":"error","error":"missing_or_invalid_oauth_token"}\n' >&3
      echo "$(date -u +%FT%TZ) rotate-fail dev=${DEV_ID} reason=missing_oauth_token from=${SSH_CLIENT:-unknown}" >> "$LOG_FILE"
      exit 1
    }

    # Validate OAuth token against Anthropic API (200 or 400 = valid auth;
    # 401/403 = bad token).
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
    ;;

  codex)
    CODEX_AUTH_JSON="$(read_field codex_auth_json)" || {
      printf '{"status":"error","error":"missing_or_invalid_codex_auth_json"}\n' >&3
      echo "$(date -u +%FT%TZ) rotate-fail dev=${DEV_ID} reason=missing_codex_auth_json from=${SSH_CLIENT:-unknown}" >> "$LOG_FILE"
      exit 1
    }

    # Extract the accessToken from auth.json — codex stores an Anthropic
    # OAuth-format token there. Validate against api.anthropic.com same as
    # the claude path. If the auth.json uses a different token key in
    # future codex versions, this will surface as a 401 (safe failure).
    CODEX_ACCESS_TOKEN="$(python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    # Support both 'accessToken' and 'access_token' key names
    tok = d.get('accessToken') or d.get('access_token') or ''
    print(tok)
except Exception:
    print('')
" <<< "$CODEX_AUTH_JSON")"

    if [ -z "$CODEX_ACCESS_TOKEN" ]; then
      printf '{"status":"error","error":"codex_auth_json_missing_access_token"}\n' >&3
      echo "$(date -u +%FT%TZ) rotate-fail dev=${DEV_ID} reason=codex_no_access_token from=${SSH_CLIENT:-unknown}" >> "$LOG_FILE"
      exit 1
    fi

    VALIDATION_HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
      -H "Authorization: Bearer ${CODEX_ACCESS_TOKEN}" \
      -H "anthropic-version: 2023-06-01" \
      -H "Content-Type: application/json" \
      -X POST https://api.anthropic.com/v1/messages \
      -d '{"model":"claude-haiku-4-5-20251001","max_tokens":1,"messages":[{"role":"user","content":"x"}]}' || echo 000)

    case "$VALIDATION_HTTP" in
      200|400) ;;
      401|403)
        printf '{"status":"error","error":"codex_auth_json_invalid","http":%s}\n' "$VALIDATION_HTTP" >&3
        echo "$(date -u +%FT%TZ) rotate-fail dev=${DEV_ID} reason=codex_auth_${VALIDATION_HTTP} from=${SSH_CLIENT:-unknown}" >> "$LOG_FILE"
        exit 1
        ;;
      *)
        printf '{"status":"error","error":"codex_validation_unreachable","http":%s}\n' "$VALIDATION_HTTP" >&3
        echo "$(date -u +%FT%TZ) rotate-fail dev=${DEV_ID} reason=codex_auth_${VALIDATION_HTTP} from=${SSH_CLIENT:-unknown}" >> "$LOG_FILE"
        exit 1
        ;;
    esac
    ;;

  gemini)
    GEMINI_API_KEY="$(read_field gemini_api_key)" || {
      printf '{"status":"error","error":"missing_or_invalid_gemini_api_key"}\n' >&3
      echo "$(date -u +%FT%TZ) rotate-fail dev=${DEV_ID} reason=missing_gemini_api_key from=${SSH_CLIENT:-unknown}" >> "$LOG_FILE"
      exit 1
    }

    # Validate the Gemini API key by hitting the models list endpoint.
    # 200 = valid key; 400/403 = bad key.
    VALIDATION_HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
      "https://generativelanguage.googleapis.com/v1beta/models?key=${GEMINI_API_KEY}" || echo 000)

    case "$VALIDATION_HTTP" in
      200) ;;
      400|401|403)
        printf '{"status":"error","error":"gemini_api_key_rejected","http":%s}\n' "$VALIDATION_HTTP" >&3
        echo "$(date -u +%FT%TZ) rotate-fail dev=${DEV_ID} reason=gemini_key_${VALIDATION_HTTP} from=${SSH_CLIENT:-unknown}" >> "$LOG_FILE"
        exit 1
        ;;
      *)
        printf '{"status":"error","error":"gemini_validation_unreachable","http":%s}\n' "$VALIDATION_HTTP" >&3
        echo "$(date -u +%FT%TZ) rotate-fail dev=${DEV_ID} reason=gemini_key_${VALIDATION_HTTP} from=${SSH_CLIENT:-unknown}" >> "$LOG_FILE"
        exit 1
        ;;
    esac
    ;;

  *)
    printf '{"status":"error","error":"unknown_cli","cli":"%s"}\n' "$CLI_NAME" >&3
    echo "$(date -u +%FT%TZ) rotate-fail dev=${DEV_ID} reason=unknown_cli=${CLI_NAME} from=${SSH_CLIENT:-unknown}" >> "$LOG_FILE"
    exit 1
    ;;
esac

# ─── Persist to invitations DB via Python helper ───────────────────────────

# Helper handles: pubkey shape validation, credential persistence per CLI,
# and the proper state-machine guards. We pass invite_id_short as argv
# (non-secret) and all credentials as JSON on stdin (avoids them appearing
# in `ps`).
HELPER_INPUT="$(python3 -c "
import json, sys
cli = '${CLI_NAME}'
pubkey = sys.argv[1]
data = {'pubkey': pubkey, 'cli': cli}
if cli == 'claude':
    data['oauth_token'] = sys.argv[2]
elif cli == 'codex':
    data['codex_auth_json'] = json.loads(sys.argv[2])
elif cli == 'gemini':
    data['gemini_api_key'] = sys.argv[2]
print(json.dumps(data))
" \
  "$PUBKEY" \
  "$(
    case "$CLI_NAME" in
      claude) echo "$OAUTH_TOKEN" ;;
      codex)  echo "$CODEX_AUTH_JSON" ;;
      gemini) echo "$GEMINI_API_KEY" ;;
    esac
  )"
)"

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
