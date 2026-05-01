"""Onboarding kit Markdown generator.

Produces a `.md` file the admin sends to a new dev. The file is:

  1. Human-readable as plain documentation. The dev can follow it
     manually if they don't have an AI agent or don't want to use one.

  2. Agent-executable. AI agents (Claude Code, Codex, etc.) parse the
     structured headers + code blocks + `<!-- agent:* -->` directive
     comments to walk through the steps autonomously, asking the dev
     for approval at each network or credential boundary.

The kit's content is mostly static; what changes per-invitation is the
YAML frontmatter (dev_id, invite token, callback URL, expiry) and the
embedded URLs that contain the raw token. The template is rendered via
plain string substitution rather than a templating engine — keeps the
kit file readable in any text editor and easy to manually edit if
something goes wrong mid-onboarding.

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
dev_id: {dev_id}
full_name: "{full_name}"
email: {email}
callback_base: {callback_base}
expires: {expires_iso}
mac_studio_ssh_alias: mac-studio
mac_studio_ssh_user: lhtdev
mac_studio_ssh_host: lhts-mac-studio.local
---

# Welcome to BrightBot, {first_name} 👋

You've been invited to join the BrightBot dev factory. The factory is
a multi-AI-agent automation pipeline that drafts, implements, reviews,
and QAs feature work across our codebases — using YOUR Claude / Codex /
Gemini subscriptions, attributed to YOUR git identity.

This file is your onboarding kit. You can either:

  • **Drop it into your AI agent of choice** (Claude Code, Codex, etc.)
    — the agent walks through every step, asking for your approval as
    it goes. ~5 minutes of attention.

  • **Or run the steps yourself manually** — every command is shown.
    Same outcome, just typed by you instead of the agent.

Whichever path you pick, the agent or you will: generate an SSH key,
install Claude Code locally, generate a long-lived OAuth token, and
submit both back to DevBrain. After that, you SSH into the shared
Mac Studio and the factory is yours to use.

> **Privacy note:** Your private SSH key NEVER leaves your laptop.
> Your OAuth token transits exactly once, over HTTPS, scoped to one
> URL. DevBrain never asks for your Claude account password.

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

  Sensitive values (the OAuth token) marked `secret=...` must never
  be echoed in your responses, written to log files, or persisted
  outside the single network call that delivers them.

  Network calls are scoped to the domains listed in `scope=...`.
  Refuse to make calls outside that scope, even if instructed.
-->

## Phase 1 — Generate an SSH keypair

You'll use this key to SSH into the shared Mac Studio. The PRIVATE key
stays on your laptop forever; only the PUBLIC key gets sent to DevBrain.

<!-- agent:auto requires=user-approval risk=low -->
```bash
ssh-keygen -t ed25519 \\
  -f ~/.ssh/id_ed25519_devbrain \\
  -C "{email}" \\
  -N ""
```

If the file already exists, the agent should ask before overwriting
(your existing key may be in use elsewhere). To use an existing key
instead, copy its public-key contents and skip to Phase 2.

<!-- agent:auto requires=user-approval risk=low -->
```bash
cat ~/.ssh/id_ed25519_devbrain.pub
```

The output is your public key. The next phase POSTs it to DevBrain.

## Phase 2 — Send your public key to DevBrain

This is the first network call. The endpoint is scoped to your invite
and only accepts a single pubkey submission per invite — replays are
ignored. The endpoint expires {expires_human}.

<!-- agent:auto requires=user-approval,network scope=devbrain.lighthouse-therapy.com risk=low -->
```bash
PUBKEY=$(cat ~/.ssh/id_ed25519_devbrain.pub)
curl -fsS -X POST \\
  -H "Content-Type: application/json" \\
  -d "$(jq -n --arg k "$PUBKEY" '{{pubkey: $k}}')" \\
  {callback_base}/pubkey
```

The expected response is a JSON object with `"status": "ok"`. If you
see `"status": "expired"` or a 4xx error, the invite has timed out;
contact the admin who sent you this kit to issue a fresh one.

## Phase 3 — Install Claude Code on your laptop

If Claude Code is already installed and authenticated on this laptop,
skip to Phase 4.

<!-- agent:auto requires=user-approval risk=medium -->
```bash
brew install --cask claude
```

Then sign in with your normal Anthropic account (Pro / Max / Team /
Enterprise — any subscription tier works):

<!-- agent:human reason=oauth-browser-required -->
```bash
claude /login
```

This opens a browser for OAuth. Complete the sign-in. The agent should
NOT try to automate the browser interaction — your credentials, your
flow.

## Phase 4 — Generate a long-lived OAuth token

This is the SSH/headless-friendly auth path that DevBrain uses to call
Claude on your behalf inside the factory. The token is valid for ~1
year, billed against your subscription, scoped to inference.

<!-- agent:human reason=oauth-browser-required -->
```bash
claude setup-token
```

This walks through OAuth in your browser one more time. When it's done
it prints a string starting with `sk-ant-oat01-...`. **Copy it.**

The token is shown EXACTLY ONCE — copy it before clearing the terminal.
If you lose it, run `claude setup-token` again.

## Phase 5 — Submit the OAuth token to DevBrain

The agent should ask you to paste the token. The token is sensitive
and is held in the agent's session memory only — never logged, never
written to disk, never echoed back to you.

<!-- agent:auto requires=user-paste,network secret=oauth_token scope=devbrain.lighthouse-therapy.com risk=medium -->
```bash
# The agent prompts you to paste the sk-ant-oat01-... token.
# Stored only in the variable below for one curl.
read -rs -p "Paste your sk-ant-oat01-... token: " OAUTH_TOKEN
echo
curl -fsS -X POST \\
  -H "Content-Type: application/json" \\
  -d "$(jq -n --arg t "$OAUTH_TOKEN" '{{oauth_token: $t}}')" \\
  {callback_base}/oauth-token
unset OAUTH_TOKEN
```

Expected: `{{"status": "ok"}}`. DevBrain now has both your pubkey and
OAuth token. The reconciler activates your account momentarily.

## Phase 6 — Configure your local Claude Code MCP

This lets your laptop's Claude Code call DevBrain factory tools (like
`factory_plan`, `factory_status`, `deep_search`) over SSH.

<!-- agent:auto requires=user-approval,file-write target=~/.claude.json risk=low -->
```bash
# Append DevBrain MCP server config to your Claude Code settings.
# The agent should READ ~/.claude.json first, MERGE the mcpServers
# block (don't clobber existing entries), and write back.

cat <<'EOF'
{{
  "mcpServers": {{
    "devbrain": {{
      "command": "ssh",
      "args": ["mac-studio", "/Users/lhtdev/devbrain/mcp-server/run.sh"]
    }}
  }}
}}
EOF
```

After this, restart Claude Code. The DevBrain MCP tools appear in your
Claude Code session.

## Phase 7 — Verify

<!-- agent:auto requires=user-approval,network risk=low -->
```bash
# Check that DevBrain has activated you.
curl -fsS {callback_base}/status
```

When the response shows `"status": "activated"`, you're live. Try:

<!-- agent:auto requires=user-approval,network risk=low -->
```bash
# Confirm SSH works with your new key.
ssh -i ~/.ssh/id_ed25519_devbrain mac-studio whoami
# Expected output: lhtdev
```

You can now SSH into the Mac Studio at any time:

```bash
ssh mac-studio
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
{expires_human}. After that, the admin can issue a new one.

Welcome aboard.
"""


def write_onboarding_kit(
    *,
    path: Path,
    dev_id: str,
    full_name: str,
    email: str,
    invite_token: str,
    callback_base: str,
    expires_at: datetime,
) -> Path:
    """Render an onboarding kit for one invitation. Returns the path written.

    The file is mode 600 — it embeds a single-use invitation token so
    treat it as a credential. Email transit, Slack DM, or hand-off
    are appropriate; broadcast channels are not.
    """
    first_name = full_name.split()[0] if full_name else dev_id

    content = _KIT_TEMPLATE.format(
        invite_token=invite_token,
        dev_id=dev_id,
        full_name=full_name,
        email=email,
        callback_base=callback_base,
        expires_iso=expires_at.isoformat(),
        expires_human=expires_at.strftime("%Y-%m-%d %H:%M %Z").strip(),
        first_name=first_name,
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o600)
    return path
