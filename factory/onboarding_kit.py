"""Onboarding kit Markdown generator.

Produces a `.md` file the admin sends to a new dev. The file is:

  1. Human-readable as plain documentation. The dev can follow it
     manually if they don't have an AI agent or don't want to use one.

  2. Agent-executable. AI agents (Claude Code, Codex, etc.) parse the
     structured headers + code blocks + `<!-- agent:* -->` directive
     comments to walk through the steps autonomously, asking the dev
     for approval at each network or credential boundary.

The kit's content is mostly static; what changes per-invitation is
the YAML frontmatter (dev_id, invite token, expiry, cli) and the
embedded TEMP SSH PRIVATE KEY that gives the dev's agent a one-shot
rotation session into the Mac Studio.

Bootstrap flow:
  1. Admin runs `devbrain setup add-dev`. DevBrain generates an
     ephemeral ed25519 keypair, stages the public half in
     ~lhtdev/.ssh/authorized_keys with strict options
     (`restrict,command="onboard_rotate.sh ...",expiry-time="..."`),
     and embeds the PRIVATE half into this kit.
  2. Admin emails the kit to the dev.
  3. Dev's agent reads the temp private key from the kit, writes it
     to ~/.ssh/devbrain-bootstrap-<dev_id> (mode 600).
  4. Agent SSHes into the Mac Studio with the temp key, sending CLI-
     specific JSON on stdin (see Phase 5 for each CLI's payload shape).
  5. The temp key's authorized_keys entry pins the SSH command to
     onboard_rotate.sh (no shell, no other capabilities possible).
     onboard_rotate.sh validates the credentials against the appropriate
     upstream API, persists the key+credential to the invitations DB,
     and self-deletes the temp authorized_keys entry.
  6. Reconciler picks up the now-ready invitation, finishes
     activation: appends the dev's PERMANENT pubkey to
     authorized_keys, stashes the CLI credential at the per-profile
     path for that CLI, populates per-profile gitconfig, fires admin
     notification.
  7. Agent deletes the temp private key file from the dev's laptop.

CLI-specific auth shapes:

  claude:
    Install:  brew install --cask claude
    Login:    claude /login  (browser OAuth)
    Token:    claude setup-token  → sk-ant-oat01-...
    Rotation: JSON {"pubkey": "...", "oauth_token": "sk-ant-oat01-..."}
    Storage:  <profile>/.claude/oauth-token

  codex:
    Install:  npm install -g @openai/codex  (binary: `codex`)
    Login:    codex login --device-auth  (device-code flow; no localhost port)
    Token:    ~/.codex/auth.json (written automatically by `codex login`)
    Rotation: JSON {"pubkey": "...", "codex_auth_json": "<contents of auth.json>"}
    Storage:  <profile>/.codex/auth.json

  gemini:
    Install:  npm install -g @google/gemini-cli  (binary: `gemini`)
    Login:    API key from aistudio.google.com (headless-friendly; no OAuth dance)
    Token:    GEMINI_API_KEY value
    Rotation: JSON {"pubkey": "...", "gemini_api_key": "AIza..."}
    Storage:  <profile>/.devbrain/env  (KEY=VALUE sourced by spawn)

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
from typing import Literal

CliName = Literal["claude", "codex", "gemini"]

VALID_CLIS: tuple[str, ...] = ("claude", "codex", "gemini")
VALID_PLATFORMS: tuple[str, ...] = ("auto", "mac", "linux", "windows")

# ─── Shared preamble (same for every CLI) ─────────────────────────────────────

_PREAMBLE = """\
---
devbrain_invite_token: {invite_token}
devbrain_invite_id_short: {invite_id_short}
dev_id: {dev_id}
full_name: "{full_name}"
email: {email}
cli: {cli}
expires: {expires_iso}
bootstrap_expires: {bootstrap_expiry_iso}
mac_studio_ssh_user: lhtdev
mac_studio_ssh_host: lhts-mac-studio.local
---

# Welcome to BrightBot, {first_name} 👋

You've been invited to join the BrightBot dev factory — Lighthouse
Therapy's multi-AI-agent automation pipeline that drafts, implements,
reviews, and QAs feature work using YOUR {cli_display_name} subscription,
attributed to YOUR git identity.

This file is your onboarding kit. You can either:

  • **Drop it into your AI agent of choice** (Claude Code, Codex, Gemini CLI, etc.)
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
> alone isn't enough. The rotation script also requires a valid credential
> (which you generate from your own {cli_display_name} account in
> Phase 4 below). Without that credential, the temp key is useless.

---

<!-- agent:plan -->
<!--
  AGENT INSTRUCTIONS — read these first, then execute the steps below.

  This document onboards the user named in the frontmatter to DevBrain
  using {cli_display_name}. Walk through the numbered phases in order.
  At each phase boundary, show the user a one-sentence summary of what
  you're about to do and ask for explicit approval before proceeding
  (yes/no). Never run the next phase without approval for THAT phase.

  Steps marked `<!-- agent:auto -->` you may execute on the user's
  behalf after approval. Steps marked `<!-- agent:human -->` require
  the user to perform an action in their browser or terminal; you must
  NOT attempt to automate them — display the command, wait for them to
  run it, and only then continue.

  Sensitive values (the bootstrap private key and the CLI credential)
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
"""

# ─── Phase 2: Install (CLI-specific) ──────────────────────────────────────────

_PHASE2_CLAUDE = """\
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
"""

_PHASE2_CODEX = """\
## Phase 2 — Install Codex

If Codex is already installed and authenticated on this laptop,
skip to Phase 3.

<!-- agent:auto requires=user-approval risk=medium -->
```bash
npm install -g @openai/codex
```

Authenticate via device-code flow (works over SSH — no localhost port needed):

<!-- agent:human reason=device-code-required -->
```bash
codex login --device-auth
```

Codex will print a URL and a one-time code. Open the URL in your browser
(on any device), enter the code, and confirm. Once the CLI reports success,
the auth token is written to `~/.codex/auth.json` automatically.
"""

_PHASE2_GEMINI = """\
## Phase 2 — Install Gemini CLI

If Gemini CLI is already installed and you have an API key, skip to Phase 3.

<!-- agent:auto requires=user-approval risk=medium -->
```bash
npm install -g @google/gemini-cli
```

Get a Gemini API key from Google AI Studio (free tier available):

<!-- agent:human reason=browser-required -->
Open https://aistudio.google.com/app/apikey in your browser, sign in with
your Google account, click "Create API key", and copy the resulting key
(it starts with `AIza...`).

You do **not** need to run `gemini auth login` — DevBrain uses the API key
directly (headless-friendly, no localhost callback port needed).
"""

# ─── Phase 3: Generate / capture credential (CLI-specific) ────────────────────

_PHASE3_CLAUDE = """\
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
"""

_PHASE3_CODEX = """\
## Phase 3 — Capture your Codex auth token

After `codex login --device-auth` completed in Phase 2, the auth token was
written automatically to `~/.codex/auth.json`.

<!-- agent:auto requires=file-read,user-approval secret=codex_auth_json risk=low -->
```bash
# Agent: read the CONTENTS of ~/.codex/auth.json into session memory.
# Label it `codex_auth_json`. Do NOT print, log, or echo the contents.
# This file is the credential DevBrain needs — treat it like a password.

cat ~/.codex/auth.json
```

Keep the JSON content in session memory — you'll send it in Phase 5.
"""

_PHASE3_GEMINI = """\
## Phase 3 — Store your Gemini API key

Hold the `AIza...` API key you copied from AI Studio in session memory.
**Do not paste it into any chat, file, or log.** The agent will use it
only in Phase 5's rotation call.

<!-- agent:human reason=user-must-paste secret=gemini_api_key risk=medium -->
Paste your Gemini API key when your agent asks for it. It will be held
in memory only and discarded after Phase 5 completes.
"""

# ─── Phase 4: Stage bootstrap key (identical for all CLIs) ────────────────────

_PHASE4 = """\
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
"""

# ─── Phase 5: Rotate (CLI-specific JSON payload) ──────────────────────────────

_PHASE5_CLAUDE = """\
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

jq -n --arg p "$PUBKEY" --arg t "$OAUTH_TOKEN" '{{pubkey: $p, oauth_token: $t}}' \\
  | ssh -i ~/.ssh/devbrain-bootstrap-{dev_id} \\
        {ssh_port_flag} \\
        -o StrictHostKeyChecking=accept-new \\
        -o UserKnownHostsFile=~/.ssh/known_hosts \\
        lhtdev@{ssh_host}

# Expected output: {{"status":"ok","dev_id":"{dev_id}","invite_id":"..."}}
```

If the response is anything other than `status: ok`, stop and surface
the error to the user. Common errors:
  - `oauth_token_rejected_by_anthropic` — the token wasn't valid; re-run `claude setup-token` and retry.
  - `no_matching_invitation_for_prefix=` — the invitation has expired or been revoked. Contact the admin who sent the kit.
"""

_PHASE5_CODEX = """\
## Phase 5 — SSH-rotate to your permanent key

This single SSH connection delivers your permanent pubkey + Codex auth
token to the Mac Studio's rotation handler. The handler validates the
token format, persists both, and self-deletes the bootstrap key entry.

<!-- agent:auto requires=user-approval,network,user-paste secret=codex_auth_json scope=lhts-mac-studio.local risk=medium -->
```bash
# Agent: read $CODEX_AUTH_JSON from session memory (collected in Phase 3).
# DO NOT log, persist, or echo it.

PUBKEY=$(cat ~/.ssh/id_ed25519_devbrain.pub)

jq -n --arg p "$PUBKEY" --argjson a "$CODEX_AUTH_JSON" '{{pubkey: $p, codex_auth_json: $a}}' \\
  | ssh -i ~/.ssh/devbrain-bootstrap-{dev_id} \\
        {ssh_port_flag} \\
        -o StrictHostKeyChecking=accept-new \\
        -o UserKnownHostsFile=~/.ssh/known_hosts \\
        lhtdev@{ssh_host}

# Expected output: {{"status":"ok","dev_id":"{dev_id}","invite_id":"..."}}
```

If the response is anything other than `status: ok`, stop and surface
the error to the user. Common errors:
  - `codex_auth_json_invalid` — the auth.json content wasn't accepted; re-run `codex login --device-auth` and retry.
  - `no_matching_invitation_for_prefix=` — the invitation has expired or been revoked. Contact the admin who sent the kit.
"""

_PHASE5_GEMINI = """\
## Phase 5 — SSH-rotate to your permanent key

This single SSH connection delivers your permanent pubkey + Gemini API
key to the Mac Studio's rotation handler. The handler validates the
API key by hitting the Gemini API, persists both, and self-deletes the
bootstrap key entry.

<!-- agent:auto requires=user-approval,network,user-paste secret=gemini_api_key scope=lhts-mac-studio.local risk=medium -->
```bash
# Agent: read $GEMINI_API_KEY from session memory (collected in Phase 3).
# DO NOT log, persist, or echo it.

PUBKEY=$(cat ~/.ssh/id_ed25519_devbrain.pub)

jq -n --arg p "$PUBKEY" --arg k "$GEMINI_API_KEY" '{{pubkey: $p, gemini_api_key: $k}}' \\
  | ssh -i ~/.ssh/devbrain-bootstrap-{dev_id} \\
        {ssh_port_flag} \\
        -o StrictHostKeyChecking=accept-new \\
        -o UserKnownHostsFile=~/.ssh/known_hosts \\
        lhtdev@{ssh_host}

# Expected output: {{"status":"ok","dev_id":"{dev_id}","invite_id":"..."}}
```

If the response is anything other than `status: ok`, stop and surface
the error to the user. Common errors:
  - `gemini_api_key_rejected` — the API key wasn't valid; get a fresh key from aistudio.google.com and retry.
  - `no_matching_invitation_for_prefix=` — the invitation has expired or been revoked. Contact the admin who sent the kit.
"""

# ─── Phase 6-8: Cleanup + MCP config + verify (identical for all CLIs) ────────

_PHASES_6_8 = """\
## Phase 6 — Cleanup

<!-- agent:auto requires=file-write target=~/.ssh/devbrain-bootstrap-{dev_id} risk=low -->
```bash
shred -u ~/.ssh/devbrain-bootstrap-{dev_id} 2>/dev/null || rm -f ~/.ssh/devbrain-bootstrap-{dev_id}
```

(macOS doesn't ship `shred` by default — `rm -f` is the fallback. The
key was useless after rotation anyway.)

## Phase 7 — Configure your local {cli_display_name} MCP

Lets your laptop's {cli_display_name} call DevBrain factory tools (factory_plan,
factory_status, deep_search) over SSH using your permanent key.

<!-- agent:auto requires=user-approval,file-write target={mcp_config_path} risk=low -->
```bash
# Agent: read {mcp_config_path}, MERGE the mcpServers block (don't clobber
# existing entries), write back.

cat <<'EOF'
{{
  "mcpServers": {{
    "devbrain": {{
      "command": "ssh",
      "args": ["-i", "~/.ssh/id_ed25519_devbrain", "-p", "{ssh_port}",
               "lhtdev@{ssh_host}",
               "/Users/lhtdev/devbrain/mcp-server/run.sh"]
    }}
  }}
}}
EOF
```

After this, restart {cli_display_name}. The DevBrain MCP tools appear in your
{cli_display_name} session.

## Phase 8 — Verify

<!-- agent:auto requires=user-approval,network risk=low -->
```bash
ssh -i ~/.ssh/id_ed25519_devbrain {ssh_port_flag} lhtdev@{ssh_host} whoami
# Expected output: lhtdev
```

You can now SSH into the Mac Studio at any time:

```bash
ssh -i ~/.ssh/id_ed25519_devbrain {ssh_port_flag} lhtdev@{ssh_host}
# (Add to ~/.ssh/config as `Host mac-studio` for ergonomics.)
```

Once on the Mac Studio, view the factory dashboard:

```bash
factory dashboard
```

Or submit a job:

```bash
factory submit "Add a no-op test that asserts True" --project devbrain --cli {cli}
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

# ─── Bootstrap private key block (appended at end of kit) ─────────────────────

_BOOTSTRAP_KEY_BLOCK = """\

<!-- The bootstrap private key below is referenced by Phase 4 above.
     agent:secret — never echo, log, or transmit outside Phase 4-5. -->

```
bootstrap_private_key:
{bootstrap_private_key}```
"""

# ─── CLI display names + MCP config paths ─────────────────────────────────────

_CLI_DISPLAY_NAMES: dict[str, str] = {
    "claude": "Claude Code",
    "codex": "Codex",
    "gemini": "Gemini CLI",
}

# The config file each CLI merges mcpServers into
_MCP_CONFIG_PATHS: dict[str, str] = {
    "claude": "~/.claude.json",
    "codex": "~/.codex/config.json",
    "gemini": "~/.gemini/settings.json",
}

# ─── Phase 0: Windows preflight (only when platform=windows) ──────────────────
#
# Verifies WSL2 + Ubuntu + apt prereqs, installs only what's missing. After
# Phase 0 completes, the dev reopens their AI agent INSIDE the WSL Ubuntu
# shell. Phases 1-8 then run as standard bash and "just work".

_PHASE0_WINDOWS = """\
## Phase 0 — Windows preflight (WSL2 + Ubuntu + apt prereqs)

Phases 1-8 of this kit assume a Linux-shaped shell (bash, ssh, jq,
`~/.ssh/`). On Windows the cleanest path is
**WSL2 + Ubuntu** — the rest of the kit then runs unchanged inside
the WSL shell.

The agent does verify-before-install: each dependency is checked first;
only missing pieces are installed.

### Step 0.1 — Verify WSL2

<!-- agent:auto requires=user-approval risk=low -->
```powershell
# Run in PowerShell.
wsl --status 2>&1
# Expected if installed: "Default Distribution: Ubuntu" or similar.
# If output is "Windows Subsystem for Linux has no installed
# distributions" or the command isn't found — install per Step 0.2.
```

### Step 0.2 — Install WSL2 + Ubuntu (only if Step 0.1 reported missing)

<!-- agent:human reason=requires-admin-elevation -->
```powershell
# Run in PowerShell as Administrator. Requires reboot afterward.
wsl --install -d Ubuntu
# Reboot when prompted, then:
#   1. Open "Ubuntu" from the Start menu
#   2. Set a Linux username + password when prompted
#   3. Once the Ubuntu shell is open, continue to Step 0.3
```

> If WSL is already installed but the default distro isn't Ubuntu, you
> can skip the install and just `wsl -d Ubuntu` from PowerShell. The
> agent should NOT clobber an existing distro choice.

### Step 0.3 — Verify Ubuntu apt prereqs

<!-- agent:auto requires=user-approval risk=low -->
```bash
# Run inside the WSL Ubuntu shell.
for cmd in jq ssh curl openssl; do
    if command -v $cmd >/dev/null 2>&1; then
        echo "✓ $cmd present"
    else
        echo "✗ $cmd missing"
    fi
done
```

### Step 0.4 — Install missing apt packages (only the ones flagged ✗ above)

<!-- agent:auto requires=user-approval,sudo risk=low -->
```bash
# Adjust the package list to ONLY the missing ones from Step 0.3.
# jq → jq, ssh → openssh-client, curl → curl, openssl → openssl
sudo apt update
sudo apt install -y <space-separated-package-names>
```

### Step 0.5 — Switch your AI agent to WSL

The remaining phases (1-8) run inside this WSL Ubuntu shell. **Reopen
your AI agent here** — close the Windows-native session and:

- **Codex / Gemini CLI:** install + run them inside WSL (Phase 2 covers this)
- **Claude Code:** install Claude Code inside WSL (Phase 2 covers this)
- **Claude Desktop / Codex Desktop apps:** Windows-native is fine, but
  the kit's bash commands need to execute via your WSL agent session

Once your agent is running inside WSL Ubuntu, continue to Phase 1.

---

"""


_PHASE2_BY_CLI: dict[str, str] = {
    "claude": _PHASE2_CLAUDE,
    "codex": _PHASE2_CODEX,
    "gemini": _PHASE2_GEMINI,
}

_PHASE3_BY_CLI: dict[str, str] = {
    "claude": _PHASE3_CLAUDE,
    "codex": _PHASE3_CODEX,
    "gemini": _PHASE3_GEMINI,
}

_PHASE5_BY_CLI: dict[str, str] = {
    "claude": _PHASE5_CLAUDE,
    "codex": _PHASE5_CODEX,
    "gemini": _PHASE5_GEMINI,
}


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
    ssh_port: int = 22,
    cli: CliName = "claude",
    platform: str = "auto",
) -> Path:
    """Render an onboarding kit for one invitation. Returns the path written.

    The file is mode 600 — it embeds a single-use bootstrap SSH private
    key (locked to the rotation handler, auto-expires) plus the
    invitation token. Treat as a credential. Email transit, Slack DM,
    or hand-off are appropriate; broadcast channels are not.

    Args:
        cli: Which AI CLI to generate the kit for. Defaults to 'claude'
             for backward compatibility. Valid values: 'claude', 'codex',
             'gemini'. Determines the Install (Phase 2), Login/Token-capture
             (Phase 3), and SSH rotation payload (Phase 5) sections.
    """
    if cli not in VALID_CLIS:
        raise ValueError(f"cli must be one of {VALID_CLIS!r}, got {cli!r}")
    if platform not in VALID_PLATFORMS:
        raise ValueError(
            f"platform must be one of {VALID_PLATFORMS!r}, got {platform!r}"
        )

    first_name = full_name.split()[0] if full_name else dev_id

    # Ensure trailing newline on the private key so the heredoc
    # cat-block produces a clean PEM-formatted file.
    if not bootstrap_private_key.endswith("\n"):
        bootstrap_private_key = bootstrap_private_key + "\n"

    fmt_args = dict(
        invite_token=invite_token,
        invite_id_short=bootstrap_invite_id_short,
        dev_id=dev_id,
        full_name=full_name,
        email=email,
        cli=cli,
        cli_display_name=_CLI_DISPLAY_NAMES[cli],
        mcp_config_path=_MCP_CONFIG_PATHS[cli],
        expires_iso=expires_at.isoformat(),
        expires_human=expires_at.strftime("%Y-%m-%d %H:%M %Z").strip(),
        bootstrap_expiry_iso=bootstrap_expiry.isoformat(),
        bootstrap_expiry_human=bootstrap_expiry.strftime("%Y-%m-%d %H:%M %Z").strip(),
        first_name=first_name,
        bootstrap_private_key=bootstrap_private_key,
        ssh_host=ssh_host,
        ssh_port=ssh_port,
        ssh_port_flag=f"-p {ssh_port}",
    )

    sections: list[str] = [_PREAMBLE]
    if platform == "windows":
        sections.append(_PHASE0_WINDOWS)
    sections += [
        _PHASE2_BY_CLI[cli],
        _PHASE3_BY_CLI[cli],
        _PHASE4,
        _PHASE5_BY_CLI[cli],
        _PHASES_6_8,
        _BOOTSTRAP_KEY_BLOCK,
    ]
    content = "".join(s.format(**fmt_args) for s in sections)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o600)
    return path
