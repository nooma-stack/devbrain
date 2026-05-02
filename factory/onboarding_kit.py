"""Onboarding kit Markdown generator.

Produces a `.md` file the admin sends to a new dev. The file is:

  1. Human-readable as plain documentation. The dev can follow it
     manually if they don't have an AI agent or don't want to use one.

  2. Agent-executable. AI agents (Claude Code, Codex, etc.) parse the
     structured headers + code blocks + `<!-- agent:* -->` directive
     comments to walk through the steps autonomously, asking the dev
     for approval at each network or credential boundary.

The kit's content is mostly static; what changes per-invitation is
the YAML frontmatter (dev_id, invite token, expiry) and the embedded
TEMP SSH PRIVATE KEY that gives the dev's agent a one-shot rotation
session into the Mac Studio.

Bootstrap flow:
  1. Admin runs `devbrain setup add-dev`. DevBrain generates an
     ephemeral ed25519 keypair, stages the public half in
     ~lhtdev/.ssh/authorized_keys with strict options
     (`restrict,command="onboard_rotate.sh ...",expiry-time="..."`),
     and embeds the PRIVATE half into this kit.
  2. Admin emails the kit to the dev.
  3. Dev's agent reads the temp private key from the kit, writes it
     to ~/.ssh/devbrain-bootstrap-<dev_id> (mode 600).
  4. Agent SSHes into the Mac Studio with the temp key, sending JSON
     {"pubkey": "<their permanent ed25519 pubkey>", "oauth_token":
     "sk-ant-oat01-..."} on stdin.
  5. The temp key's authorized_keys entry pins the SSH command to
     onboard_rotate.sh (no shell, no other capabilities possible).
     onboard_rotate.sh validates the OAuth token against
     api.anthropic.com (two-factor: temp key + valid OAuth token from
     dev's actual claude.com account), persists pubkey+token to the
     invitations DB, and self-deletes the temp authorized_keys entry.
  6. Reconciler picks up the now-ready invitation, finishes
     activation: appends the dev's PERMANENT pubkey to
     authorized_keys, stashes oauth-token, populates per-profile
     gitconfig, fires admin notification.
  7. Agent deletes the temp private key file from the dev's laptop.

Agent directive vocabulary (placed inside HTML comments so they don't
render in the visible Markdown):

  agent:auto              The agent may execute the next code block
                          after asking the user for approval.
  agent:human             The next code block requires human action
                          (browser-based OAuth, etc.). The agent
                          prompts the user to run it manually and
                          waits for them to paste back the result.
  requires=...            Comma-separated capability hints —
                          user-approval, network, file-write,
                          user-paste, browser, etc.
  scope=<host>            Network calls are scoped to this domain;
                          agent should refuse if asked to call
                          anything else under the same step.
  secret=<name>           The user input is sensitive (a token) and
                          must never be logged, persisted, or echoed
                          back outside the single network call.
  risk=low|medium|high    Severity hint for the agent's approval
                          prompt to the user.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

# The template is intentionally a single triple-quoted string. Easy to
# review as a unit, easy to translate later if needed (the agent
# directives are language-neutral; the visible English prose can be
# replaced without touching the structure).
_KIT_TEMPLATE = """---
devbrain_invite_token: {invite_token}
devbrain_invite_id_short: {invite_id_short}
dev_id: {dev_id}
full_name: "{full_name}"
email: {email}
expires: {expires_iso}
bootstrap_expires: {bootstrap_expiry_iso}
mac_studio_ssh_user: lhtdev
mac_studio_ssh_host: lhts-mac-studio.local
---

# Welcome to BrightBot, {first_name} 👋

You've been invited to join the BrightBot dev factory — Lighthouse
Therapy's multi-AI-agent automation pipeline that drafts, implements,
reviews, and QAs feature work using YOUR Claude / Codex / Gemini
subscriptions, attributed to YOUR git identity.

This file is your onboarding kit. You can either:

  • **Drop it into your AI agent of choice** (Claude Code, Codex, etc.)
    — the agent walks through every step, asking for your approval as
    it goes. ~5 minutes of attention.

  • **Or run the steps yourself manually** — every command is shown.
    Same outcome, just typed by you instead of the agent.

> **Privacy note:** The temp SSH key embedded below is single-use.
> It can ONLY run a rotation script (cannot get a shell, cannot read
> files, cannot do anything else). It auto-expires {bootstrap_expiry_human}.
> After you complete onboarding, the key auto-deletes from the
> Mac Studio.

> **Two-factor security:** Even if this email leaks, the temp key
> alone isn't enough. The rotation script also requires a valid Claude
> OAuth token (which you generate from your own claude.com account in
> Phase 4 below). Without that token, the temp key is useless.

---

<!-- agent:plan -->
<!--
  AGENT INSTRUCTIONS — read these first, then execute the steps below.

  This document onboards the user named in the frontmatter to DevBrain.
  Walk through the numbered phases in order. At each phase boundary,
  show the user a one-sentence summary of what you're about to do and
  ask for explicit approval before proceeding (yes/no). Never run the
  next phase without approval for THAT phase.

  Steps marked `<!-- agent:auto -->` you may execute on the user's
  behalf after approval. Steps marked `<!-- agent:human -->` require
  the user to perform an action in their browser; you must NOT attempt
  to automate them — display the command, wait for them to run it,
  and only then continue.

  Sensitive values (the bootstrap private key and the OAuth token)
  marked `secret=...` must never be echoed in your responses, written
  to log files, or persisted outside the single rotation invocation.
  After Phase 5 completes, DELETE the bootstrap private key file from
  the user's filesystem.
-->

## Phase 1 — Generate your permanent SSH keypair

This key stays on YOUR laptop and is what the Mac Studio will trust
going forward. The temp key embedded in this kit is for one-shot
delivery only.

<!-- agent:auto requires=user-approval risk=low -->
```bash
ssh-keygen -t ed25519 \\
  -f ~/.ssh/id_ed25519_devbrain \\
  -C "{email}" \\
  -N ""
```

If the file already exists, the agent should ask before overwriting.

## Phase 2 — Install Claude Code

If Claude Code is already installed and authenticated on this laptop,
skip to Phase 3.

<!-- agent:auto requires=user-approval risk=medium -->
```bash
brew install --cask claude
```

Sign in with your Anthropic account (Pro / Max / Team / Enterprise):

<!-- agent:human reason=oauth-browser-required -->
```bash
claude /login
```

This opens a browser for OAuth. Complete the sign-in.

## Phase 3 — Generate a long-lived OAuth token

DevBrain uses this to call Claude on your behalf inside the factory.
The token is valid for ~1 year, billed against YOUR subscription.

<!-- agent:human reason=oauth-browser-required -->
```bash
claude setup-token
```

Copy the `sk-ant-oat01-...` string. **Do not share it anywhere except
the rotation step below.** The agent will hold it in memory only and
discard after Phase 5.

## Phase 4 — Stage the bootstrap SSH key locally

The bootstrap key below is single-use, locked to a rotation script,
and auto-expires {bootstrap_expiry_human}.

<!-- agent:auto requires=file-write,user-approval target=~/.ssh/devbrain-bootstrap-{dev_id} secret=bootstrap_private_key risk=medium -->
```bash
# Agent: write the contents of `bootstrap_private_key` (below) to
# ~/.ssh/devbrain-bootstrap-{dev_id}, set mode 0600. Do NOT cat to
# stdout, do NOT add to ssh-agent, do NOT commit anywhere.

# After Phase 5 completes successfully, the agent will delete this
# file. The bootstrap key has no value beyond this single rotation.

cat > ~/.ssh/devbrain-bootstrap-{dev_id} <<'BOOTSTRAP_KEY_END'
{bootstrap_private_key}BOOTSTRAP_KEY_END

chmod 600 ~/.ssh/devbrain-bootstrap-{dev_id}
```

## Phase 5 — SSH-rotate to your permanent key

This single SSH connection delivers your permanent pubkey + OAuth
token to the Mac Studio's rotation handler. The handler validates
the OAuth token by hitting api.anthropic.com (two-factor check),
persists both, and self-deletes the bootstrap key entry.

<!-- agent:auto requires=user-approval,network,user-paste secret=oauth_token scope=lhts-mac-studio.local risk=medium -->
```bash
# Agent: read $OAUTH_TOKEN from session memory (collected in Phase 3).
# DO NOT log, persist, or echo it.

PUBKEY=$(cat ~/.ssh/id_ed25519_devbrain.pub)

ssh -i ~/.ssh/devbrain-bootstrap-{dev_id} \\
    -o StrictHostKeyChecking=accept-new \\
    -o UserKnownHostsFile=~/.ssh/known_hosts \\
    lhtdev@{ssh_host} \\
    < <(jq -n --arg p "$PUBKEY" --arg t "$OAUTH_TOKEN" '{{pubkey: $p, oauth_token: $t}}')

# Expected output: {{"status":"ok","dev_id":"{dev_id}","invite_id":"..."}}
```

If the response is anything other than `status: ok`, stop and surface
the error to the user. Common errors:
  - `oauth_token_rejected_by_anthropic` — the token wasn't valid; re-run `claude setup-token` and retry.
  - `no_matching_invitation_for_prefix=` — the invitation has expired or been revoked. Contact the admin who sent the kit.

## Phase 6 — Cleanup

<!-- agent:auto requires=file-write target=~/.ssh/devbrain-bootstrap-{dev_id} risk=low -->
```bash
shred -u ~/.ssh/devbrain-bootstrap-{dev_id} 2>/dev/null || rm -f ~/.ssh/devbrain-bootstrap-{dev_id}
```

(macOS doesn't ship `shred` by default — `rm -f` is the fallback. The
key was useless after rotation anyway.)

## Phase 7 — Configure your local Claude Code MCP

Lets your laptop's Claude Code call DevBrain factory tools (factory_plan,
factory_status, deep_search) over SSH using your permanent key.

<!-- agent:auto requires=user-approval,file-write target=~/.claude.json risk=low -->
```bash
# Agent: read ~/.claude.json, MERGE the mcpServers block (don't clobber
# existing entries), write back.

cat <<'EOF'
{{
  "mcpServers": {{
    "devbrain": {{
      "command": "ssh",
      "args": ["-i", "~/.ssh/id_ed25519_devbrain", "lhtdev@{ssh_host}",
               "/Users/lhtdev/devbrain/mcp-server/run.sh"]
    }}
  }}
}}
EOF
```

After this, restart Claude Code. The DevBrain MCP tools appear in your
Claude Code session.

## Phase 8 — Verify

<!-- agent:auto requires=user-approval,network risk=low -->
```bash
ssh -i ~/.ssh/id_ed25519_devbrain lhtdev@{ssh_host} whoami
# Expected output: lhtdev
```

You can now SSH into the Mac Studio at any time:

```bash
ssh -i ~/.ssh/id_ed25519_devbrain lhtdev@{ssh_host}
# (Add to ~/.ssh/config as `Host mac-studio` for ergonomics.)
```

Once on the Mac Studio, view the factory dashboard:

```bash
factory dashboard
```

Or submit a job:

```bash
factory submit "Add a no-op test that asserts True" --project devbrain --cli claude
```

---

## You're done!

DevBrain has notified the admin that you've completed onboarding. Ping
them if you don't get a confirmation message within a few minutes.

If anything broke, the kit and your invite token are reusable until
{expires_human}, BUT the bootstrap SSH key expires earlier
({bootstrap_expiry_human}). After bootstrap expiry, the admin can
issue a fresh kit.

Welcome aboard.
"""


def write_onboarding_kit(
    *,
    path: Path,
    dev_id: str,
    full_name: str,
    email: str,
    invite_token: str,
    callback_base: str,  # kept for backward-compat; unused in temp-key flow
    expires_at: datetime,
    bootstrap_private_key: str,
    bootstrap_invite_id_short: str,
    bootstrap_expiry: datetime,
    ssh_host: str = "lhts-mac-studio.local",
) -> Path:
    """Render an onboarding kit for one invitation. Returns the path written.

    The file is mode 600 — it embeds a single-use bootstrap SSH private
    key (locked to the rotation handler, auto-expires) plus the
    invitation token. Treat as a credential. Email transit, Slack DM,
    or hand-off are appropriate; broadcast channels are not.
    """
    first_name = full_name.split()[0] if full_name else dev_id

    # Ensure trailing newline on the private key so the heredoc
    # cat-block produces a clean PEM-formatted file.
    if not bootstrap_private_key.endswith("\n"):
        bootstrap_private_key = bootstrap_private_key + "\n"

    content = _KIT_TEMPLATE.format(
        invite_token=invite_token,
        invite_id_short=bootstrap_invite_id_short,
        dev_id=dev_id,
        full_name=full_name,
        email=email,
        expires_iso=expires_at.isoformat(),
        expires_human=expires_at.strftime("%Y-%m-%d %H:%M %Z").strip(),
        bootstrap_expiry_iso=bootstrap_expiry.isoformat(),
        bootstrap_expiry_human=bootstrap_expiry.strftime("%Y-%m-%d %H:%M %Z").strip(),
        first_name=first_name,
        bootstrap_private_key=bootstrap_private_key,
        ssh_host=ssh_host,
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o600)
    return path
