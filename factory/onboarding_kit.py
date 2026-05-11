"""Onboarding kit Markdown generator (server-side-auth flow).

Produces a `.md` file the admin sends to a new dev. The file is:

  1. Human-readable as plain documentation. The dev can follow it
     manually if they don't have an AI agent or don't want to use one.

  2. Agent-executable. AI agents (Claude Desktop, Codex Desktop, the
     CLI variants) parse the structured headers + code blocks +
     `<!-- agent:* -->` directive comments to walk through the steps
     autonomously, asking the dev for approval at each network or
     credential boundary.

The kit's content is mostly static; what changes per-invitation is
the YAML frontmatter (dev_id, invite token, expiry, cli, agent-app,
SSH host fingerprint) and the embedded TEMP SSH PRIVATE KEY that
gives the dev's agent a one-shot rotation session into the Mac Studio.

Architecture (server-side-auth flow, post 2026-05-07 redesign — see
docs/plans/2026-05-07-onboarding-server-side-auth-design.md):

  Phase 0  Trust banner — issuer, intent, host fingerprint, ask user
           to confirm before proceeding.
  Phase 1  Environment check + dep install. Bash for macOS/Linux,
           PowerShell for Windows (built-in OpenSSH; no WSL/Git Bash).
  Phase 2  Generate permanent SSH keypair locally.
  Phase 3  Stage bootstrap (one-shot, expiring) SSH key locally.
  Phase 4  Rotate permanent pubkey to Mac Studio. Payload is pubkey-
           only — no subscription credential transits the dev's box.
  Phase 5  SSH into the dev's profile on Mac Studio and run
           `devbrain login`. The wrapper invokes the appropriate
           per-CLI auth flow server-side; the auth token is generated
           and stashed inside the profile dir on the Mac Studio. It
           NEVER returns to the dev's machine.
  Phase 6  MCP wire-up + SSH verify.

Three issuance axes — admin specifies what's known at `devbrain
add-dev` time:

  --cli         claude | codex | gemini
  --platform    mac | linux | windows | auto
  --agent-app   claude-desktop | codex-desktop | gemini-desktop |
                claude-cli | codex-cli | gemini-cli | auto

Each axis defaults to `auto`. When auto, the kit branches on that
dimension and the agent navigates at runtime. When specified, the
kit is tailored.

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
VALID_AGENT_APPS: tuple[str, ...] = (
    "auto",
    "claude-desktop", "codex-desktop", "gemini-desktop",
    "claude-cli", "codex-cli", "gemini-cli",
)

_CLI_DISPLAY_NAMES: dict[str, str] = {
    "claude": "Claude Code",
    "codex": "Codex",
    "gemini": "Gemini CLI",
}

_AGENT_APP_DISPLAY_NAMES: dict[str, str] = {
    "auto": "your AI agent",
    "claude-desktop": "Claude Desktop",
    "codex-desktop": "Codex Desktop",
    "gemini-desktop": "Gemini Desktop",
    "claude-cli": "Claude Code (CLI)",
    "codex-cli": "Codex (CLI)",
    "gemini-cli": "Gemini (CLI)",
}

# Where each AI agent app reads MCP server config from
_MCP_CONFIG_PATHS: dict[str, str] = {
    "claude": "~/.claude.json",
    "codex": "~/.codex/config.json",
    "gemini": "~/.gemini/settings.json",
}

# ─── Frontmatter / preamble ───────────────────────────────────────────────────

_PREAMBLE = """\
---
devbrain_invite_token: {invite_token}
devbrain_invite_id_short: {invite_id_short}
dev_id: {dev_id}
full_name: "{full_name}"
email: {email}
cli: {cli}
agent_app: {agent_app}
platform: {platform}
expires: {expires_iso}
bootstrap_expires: {bootstrap_expiry_iso}
mac_studio_ssh_user: lhtdev
mac_studio_ssh_host: {ssh_host}
mac_studio_ssh_port: {ssh_port}
mac_studio_host_fingerprint: "{ssh_host_fingerprint}"
---

# DevBrain onboarding — {full_name}

Welcome aboard. This kit walks your AI agent through connecting your
machine to Lighthouse Therapy's DevBrain dev factory, then triggers
your AI subscription auth on the Mac Studio (server-side — your auth
token never transits this machine).

Your AI subscription: **{cli_display_name}**.
Your dev id: **{dev_id}**.
Kit expires: {expires_human}.

"""

# ─── Phase 0: Trust banner ────────────────────────────────────────────────────

_PHASE0_TRUST = """\
## Phase 0 — Verification (read this first)

This onboarding kit was issued by:

| Field | Value |
|---|---|
| **Issuer** | Lighthouse Therapy / DevBrain |
| **Sender domain** | lighthouse-therapy.com (delivered via Google Workspace DWD) |
| **Issued at** | {expires_iso} (kit expiry) |
| **Invitation ID** | `{invite_id_short}` (full UUID in YAML frontmatter above) |
| **Target server** | `{ssh_host}` on port `{ssh_port}` |
| **SSH host key fingerprint** | `{ssh_host_fingerprint}` |

### What this kit will do

1. Generate an SSH keypair on this machine (your permanent dev key,
   stays local).
2. Stage a one-shot bootstrap SSH key (embedded at the bottom of this
   file) that is locked to a single server-side rotation script and
   auto-expires {bootstrap_expiry_human}.
3. Use the bootstrap key to deliver your permanent public key to the
   Mac Studio.
4. SSH into your profile on the Mac Studio and run `devbrain login`,
   which generates your AI subscription's auth token *on the server*.
   The token never leaves the Mac Studio.
5. Wire up your local AI agent's MCP config so it can call DevBrain
   factory tools over SSH.

No long-lived credentials transit this machine. No persistent network
listeners are opened locally. The bootstrap key is rendered useless
after first use.

### Agent: ask the user to confirm before proceeding

<!-- agent:human reason=intent-confirmation risk=medium -->

Before running any commands, ask the user to confirm:

- They are expecting this onboarding kit from their employer (Lighthouse
  Therapy).
- The Invitation ID above (`{invite_id_short}`) matches what the admin
  who sent this kit said it would be.
- The Mac Studio SSH host fingerprint above matches what the admin
  shared out-of-band (this can also be verified on first SSH connect
  by comparing to the prompt your SSH client shows).

If the user can't confirm any of the above, **STOP** and have the user
contact the admin who issued the kit at `patrick@lighthouse-therapy.com`.

"""

# ─── Phase 1: Environment check + dep install ────────────────────────────────

_PHASE1_HEADER = """\
## Phase 1 — Environment check + dependency install

This kit assumes a working SSH client (`ssh`, `ssh-keygen`) on your
machine. Most macOS and Linux installations have these built in;
Windows 10+ ships them as an optional feature.

The agent should detect your platform and run the matching block below.

"""

_PHASE1_BASH = """\
### macOS / Linux (bash / zsh)

<!-- agent:auto requires=user-approval risk=low -->
```bash
# Verify ssh + ssh-keygen are present.
for cmd in ssh ssh-keygen; do
  if command -v "$cmd" >/dev/null 2>&1; then
    echo "✓ $cmd present"
  else
    echo "✗ $cmd MISSING"
  fi
done
```

If either is missing on Linux, install via your distro's package manager:

<!-- agent:auto requires=user-approval,sudo risk=low -->
```bash
# Debian/Ubuntu
sudo apt update && sudo apt install -y openssh-client

# Fedora/RHEL
sudo dnf install -y openssh-clients

# Alpine
sudo apk add openssh-client
```

(macOS ships `ssh` and `ssh-keygen` by default; nothing to install.)

"""

_PHASE1_POWERSHELL = """\
### Windows (PowerShell)

Windows 10+ ships the OpenSSH client (`ssh.exe`, `ssh-keygen.exe`) as
an optional feature. Verify it's enabled, and if not, enable it.

<!-- agent:auto requires=user-approval risk=low -->
```powershell
# Verify the OpenSSH client is installed and enabled.
Get-WindowsCapability -Online -Name 'OpenSSH.Client*' |
  Select-Object Name, State
# State should be 'Installed'.
```

If `State` shows `NotPresent`, install it (this requires admin
elevation but is fast — no reboot needed):

<!-- agent:human reason=requires-admin-elevation risk=low -->
```powershell
# Run in an elevated PowerShell (Run as Administrator).
Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0
```

After install (or if already present), verify:

<!-- agent:auto requires=user-approval risk=low -->
```powershell
ssh -V         # prints OpenSSH version
ssh-keygen -?  # prints usage
```

The remaining phases run from a regular (non-elevated) PowerShell
session.

"""

# ─── Phase 2: Generate permanent SSH keypair ─────────────────────────────────

_PHASE2_HEADER = """\
## Phase 2 — Generate your permanent SSH keypair

This key stays on YOUR machine and is what the Mac Studio will trust
going forward. The bootstrap key embedded later in this file is for
one-shot delivery only.

"""

_PHASE2_BASH = """\
### macOS / Linux

<!-- agent:auto requires=file-write,user-approval risk=low -->
```bash
ssh-keygen -t ed25519 \\
  -f ~/.ssh/id_ed25519_devbrain \\
  -C "{email}" \\
  -N ""
```

If the file already exists, the agent should ask before overwriting.

"""

_PHASE2_POWERSHELL = """\
### Windows (PowerShell)

<!-- agent:auto requires=file-write,user-approval risk=low -->
```powershell
# Ensure ~/.ssh exists with correct ACL
$sshDir = Join-Path $env:USERPROFILE ".ssh"
New-Item -ItemType Directory -Path $sshDir -Force | Out-Null

ssh-keygen -t ed25519 `
  -f "$sshDir\\id_ed25519_devbrain" `
  -C "{email}" `
  -N '""'
```

If the file already exists, the agent should ask before overwriting.

"""

# ─── Phase 3: Stage bootstrap SSH key ────────────────────────────────────────

_PHASE3_HEADER = """\
## Phase 3 — Stage the bootstrap SSH key locally

The bootstrap key (embedded at the bottom of this file under
`bootstrap_private_key:`) is single-use, locked server-side to the
rotation script, and auto-expires {bootstrap_expiry_human}.

"""

_PHASE3_BASH = """\
### macOS / Linux

<!-- agent:auto requires=file-write,user-approval target=~/.ssh/devbrain-bootstrap-{dev_id} secret=bootstrap_private_key risk=medium -->
```bash
# Agent: write the contents of `bootstrap_private_key` (at the bottom
# of this file) to ~/.ssh/devbrain-bootstrap-{dev_id} and set mode 0600.
# Do NOT cat to stdout, do NOT add to ssh-agent, do NOT commit.

cat > ~/.ssh/devbrain-bootstrap-{dev_id} <<'BOOTSTRAP_KEY_END'
{bootstrap_private_key}BOOTSTRAP_KEY_END

chmod 600 ~/.ssh/devbrain-bootstrap-{dev_id}
```

"""

_PHASE3_POWERSHELL = """\
### Windows (PowerShell)

<!-- agent:auto requires=file-write,user-approval target=~/.ssh/devbrain-bootstrap-{dev_id} secret=bootstrap_private_key risk=medium -->
```powershell
# Agent: write the contents of `bootstrap_private_key` (at the bottom
# of this file) to a per-user file under ~/.ssh and tighten the ACL so
# only the current user can read it. ssh.exe refuses keys with looser
# permissions on Windows.

$keyPath = Join-Path $env:USERPROFILE ".ssh\\devbrain-bootstrap-{dev_id}"

# Replace the placeholder block here with the bootstrap_private_key
# content (between BEGIN OPENSSH PRIVATE KEY and END OPENSSH PRIVATE KEY,
# inclusive of those markers).
@'
{bootstrap_private_key}'@ | Set-Content -Path $keyPath -NoNewline -Encoding ASCII

# Lock down the ACL: remove inheritance, grant only the current user
icacls $keyPath /inheritance:r | Out-Null
icacls $keyPath /grant:r ("{{0}}:(R)" -f $env:USERNAME) | Out-Null
```

"""

# ─── Phase 4: Rotate permanent pubkey to server ──────────────────────────────

_PHASE4_HEADER = """\
## Phase 4 — Deliver your permanent public key to the Mac Studio

This single SSH connection delivers your permanent SSH **public key**
(only — no auth token) to the rotation handler. The handler persists
your pubkey, marks the invitation ready for activation, and self-deletes
the bootstrap key entry from `authorized_keys`.

Your AI subscription auth happens server-side in Phase 5 — nothing
about it transits this connection.

"""

_PHASE4_BASH = """\
### macOS / Linux

<!-- agent:auto requires=user-approval,network scope={ssh_host} risk=medium -->
```bash
PUBKEY=$(cat ~/.ssh/id_ed25519_devbrain.pub)

printf '{{"pubkey":"%s"}}' "$PUBKEY" \\
  | ssh -i ~/.ssh/devbrain-bootstrap-{dev_id} \\
        -p {ssh_port} \\
        -o StrictHostKeyChecking=accept-new \\
        -o UserKnownHostsFile=~/.ssh/known_hosts \\
        lhtdev@{ssh_host}

# Expected response:
#   {{"status":"ok","dev_id":"{dev_id}","invite_id":"..."}}
```

"""

_PHASE4_POWERSHELL = """\
### Windows (PowerShell)

<!-- agent:auto requires=user-approval,network scope={ssh_host} risk=medium -->
```powershell
$pubkey = (Get-Content -Raw "$env:USERPROFILE\\.ssh\\id_ed25519_devbrain.pub").Trim()
$payload = @{{ pubkey = $pubkey }} | ConvertTo-Json -Compress

$payload | ssh `
  -i "$env:USERPROFILE\\.ssh\\devbrain-bootstrap-{dev_id}" `
  -p {ssh_port} `
  -o StrictHostKeyChecking=accept-new `
  -o UserKnownHostsFile="$env:USERPROFILE\\.ssh\\known_hosts" `
  "lhtdev@{ssh_host}"

# Expected response:
#   {{"status":"ok","dev_id":"{dev_id}","invite_id":"..."}}
```

"""

_PHASE4_FOOTER = """\
If the response is anything other than `status: ok`, **STOP** and
surface the error to the user. Common errors:

- `pubkey_unsafe` — your SSH pubkey didn't match the expected shape;
  re-run Phase 2 to regenerate it.
- `no_matching_invitation_for_prefix=` — the invitation has expired or
  been revoked. Contact the admin who sent the kit.

"""

# ─── Phase 5: Server-side `devbrain login` ───────────────────────────────────

_PHASE5_BY_CLI: dict[str, str] = {
    "claude": """\
## Phase 5 — Issue your Claude OAuth token (server-side)

You SSH into your profile on the Mac Studio and run `devbrain login`.
That command runs `claude setup-token` server-side; you'll see an
OAuth URL printed. Open the URL **in your local browser**, sign in
with your Anthropic account (Pro / Max / Team / Enterprise), copy the
verification code from the post-signin page, and paste it back into
this SSH session. The resulting token is stashed at
`<profile>/.claude/oauth-token` on the Mac Studio. It never returns
to this machine.

<!-- agent:auto requires=user-approval,network,user-paste secret=oauth_verification_code scope={ssh_host} risk=medium -->
""",
    "codex": """\
## Phase 5 — Authenticate Codex via device-code (server-side)

You SSH into your profile on the Mac Studio and run `devbrain login`.
That command runs `codex login --device-auth` server-side; you'll see
a verification URL and one-time code printed. Open the URL **in your
local browser**, enter the code, and confirm. The resulting auth.json
is written at `<profile>/.codex/auth.json` on the Mac Studio. It
never returns to this machine.

<!-- agent:auto requires=user-approval,network,user-paste scope={ssh_host} risk=medium -->
""",
    "gemini": """\
## Phase 5 — Provide your Gemini API key (server-side)

You SSH into your profile on the Mac Studio and run `devbrain login`.
That command will prompt you to paste your Gemini API key. Get one
from https://aistudio.google.com/app/apikey (free tier is fine), open
the URL **in your local browser**, copy the key (starts with `AIza`),
and paste it into the SSH prompt. The key is stashed at
`<profile>/.devbrain/env` on the Mac Studio. It never returns to
this machine.

<!-- agent:auto requires=user-approval,network,user-paste secret=gemini_api_key scope={ssh_host} risk=medium -->
""",
}

_PHASE5_COMMAND = """\
```bash
ssh -i ~/.ssh/id_ed25519_devbrain \\
    -p {ssh_port} \\
    -o StrictHostKeyChecking=accept-new \\
    -t lhtdev@{ssh_host} \\
    devbrain login --dev {dev_id} --cli {cli}
```

Windows PowerShell equivalent:

```powershell
ssh -i "$env:USERPROFILE\\.ssh\\id_ed25519_devbrain" `
    -p {ssh_port} `
    -o StrictHostKeyChecking=accept-new `
    -t "lhtdev@{ssh_host}" `
    devbrain login --dev {dev_id} --cli {cli}
```

The `-t` flag is required: `devbrain login` is interactive (you'll be
asked to paste a code, or an API key, depending on your CLI). The
agent should keep your SSH session open and help you copy/paste
between your local browser and the SSH prompt.

After this command exits with `success: true`, your dev profile on
the Mac Studio has the credential it needs to spawn {cli_display_name}
on your behalf. The token never came back through this connection.

"""

# ─── Phase 6: MCP wire-up + verify (per-CLI; same on bash/PowerShell) ────────

_PHASE6_HEADER = """\
## Phase 6 — Wire up your local agent's MCP config + verify SSH

This step lets your **local** AI agent ({agent_app_display}) call DevBrain
factory tools (factory_plan, factory_status, deep_search) over SSH
using your permanent key.

"""

_PHASE6_BASH = """\
### macOS / Linux

<!-- agent:auto requires=user-approval,file-write target={mcp_config_path} risk=low -->
```bash
# Agent: read {mcp_config_path}, MERGE the mcpServers block (don't
# clobber other entries), write back.

cat <<'EOF'
{{
  "mcpServers": {{
    "devbrain": {{
      "command": "ssh",
      "args": ["-i", "~/.ssh/id_ed25519_devbrain", "-p", "{ssh_port}",
               "lhtdev@{ssh_host}",
               "env", "DEVBRAIN_DEV_ID={dev_id}",
               "/Users/lhtdev/devbrain/mcp-server/run.sh"]
    }}
  }}
}}
EOF
```

After this, restart {agent_app_display}. The DevBrain MCP tools should
appear in your session.

### Verify SSH

<!-- agent:auto requires=user-approval,network scope={ssh_host} risk=low -->
```bash
ssh -i ~/.ssh/id_ed25519_devbrain -p {ssh_port} lhtdev@{ssh_host} whoami
# Expected output: lhtdev
```

You can also add the host to `~/.ssh/config` for ergonomics:

```
Host mac-studio
  HostName {ssh_host}
  Port {ssh_port}
  User lhtdev
  IdentityFile ~/.ssh/id_ed25519_devbrain
```

"""

_PHASE6_POWERSHELL = """\
### Windows (PowerShell)

<!-- agent:auto requires=user-approval,file-write target={mcp_config_path} risk=low -->
```powershell
# Agent: read {mcp_config_path} (Windows path expansion), merge the
# mcpServers block, write back. ConvertFrom-Json + ConvertTo-Json
# handle the merge cleanly.

$cfgPath = "{mcp_config_path}".Replace("~", $env:USERPROFILE) -replace "/", "\\"
$cfg = if (Test-Path $cfgPath) {{
  Get-Content -Raw $cfgPath | ConvertFrom-Json -AsHashtable
}} else {{ @{{}} }}

if (-not $cfg.mcpServers) {{ $cfg.mcpServers = @{{}} }}
$cfg.mcpServers.devbrain = @{{
  command = "ssh"
  args = @("-i", "$env:USERPROFILE\\.ssh\\id_ed25519_devbrain",
           "-p", "{ssh_port}",
           "lhtdev@{ssh_host}",
           "env", "DEVBRAIN_DEV_ID={dev_id}",
           "/Users/lhtdev/devbrain/mcp-server/run.sh")
}}

$cfg | ConvertTo-Json -Depth 10 | Set-Content -Path $cfgPath -Encoding UTF8
```

After this, restart {agent_app_display}. The DevBrain MCP tools should
appear in your session.

### Verify SSH

<!-- agent:auto requires=user-approval,network scope={ssh_host} risk=low -->
```powershell
ssh -i "$env:USERPROFILE\\.ssh\\id_ed25519_devbrain" -p {ssh_port} `
    "lhtdev@{ssh_host}" whoami
# Expected output: lhtdev
```

"""

_PHASE7_CLEANUP = """\
## Phase 7 — Cleanup (you're done!)

The bootstrap key is now useless (the server self-deleted its
`authorized_keys` entry on Phase 4 success and the key file's expiry
has rolled past). Remove it from this machine:

### macOS / Linux

<!-- agent:auto requires=file-write target=~/.ssh/devbrain-bootstrap-{dev_id} risk=low -->
```bash
shred -u ~/.ssh/devbrain-bootstrap-{dev_id} 2>/dev/null \\
  || rm -f ~/.ssh/devbrain-bootstrap-{dev_id}
```

### Windows

<!-- agent:auto requires=file-write target=~/.ssh/devbrain-bootstrap-{dev_id} risk=low -->
```powershell
Remove-Item -Force "$env:USERPROFILE\\.ssh\\devbrain-bootstrap-{dev_id}"
```

---

## You're done!

DevBrain has notified the admin that you're activated. Try submitting
a test job from your local agent:

```
factory submit "Add a no-op test that asserts True" --project devbrain --cli {cli}
```

Or run `factory dashboard` after SSHing into the Mac Studio to see the
factory's job queue.

If anything broke, the kit is reusable until {expires_human}, BUT the
bootstrap SSH key expires earlier ({bootstrap_expiry_human}). After
bootstrap expiry, the admin can issue a fresh kit.

"""

# ─── Bootstrap private key block ──────────────────────────────────────────────

_BOOTSTRAP_KEY_BLOCK = """\

<!-- The bootstrap private key below is referenced by Phase 3 above.
     agent:secret — never echo, log, or transmit outside Phase 3-4. -->

```
bootstrap_private_key:
{bootstrap_private_key}```
"""


# ─── Section assembly helpers ─────────────────────────────────────────────────

def _select_shell_variants(platform: str) -> tuple[bool, bool]:
    """Return (include_bash, include_powershell) given a platform."""
    if platform == "windows":
        return (False, True)
    if platform in ("mac", "linux"):
        return (True, False)
    # auto — include both
    return (True, True)


def _phase_block(header: str, bash: str, powershell: str, platform: str) -> str:
    """Assemble a phase by stitching header + selected shell variants."""
    include_bash, include_ps = _select_shell_variants(platform)
    parts = [header]
    if include_bash:
        parts.append(bash)
    if include_ps:
        parts.append(powershell)
    return "".join(parts)


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
    ssh_host_fingerprint: str = "",
    cli: CliName = "claude",
    platform: str = "auto",
    agent_app: str = "auto",
) -> Path:
    """Render an onboarding kit for one invitation. Returns the path written.

    Args:
        cli: AI subscription the dev's factory work runs against
             ('claude' / 'codex' / 'gemini'). Drives Phase 5's
             server-side auth flow.
        platform: Dev's local OS ('mac', 'linux', 'windows', 'auto').
                  Drives which shell variants (bash / PowerShell)
                  appear in the kit. 'auto' includes both.
        agent_app: Which AI agent app the dev will use to consume this
                   kit ('claude-desktop', 'codex-desktop',
                   'gemini-desktop', 'claude-cli', 'codex-cli',
                   'gemini-cli', 'auto'). Affects framing language;
                   functional behavior is identical across choices.
        ssh_host_fingerprint: SHA256 fingerprint of the Mac Studio's
                              SSH host pubkey, included in Phase 0
                              for the dev to verify on first connect.
                              Empty string means "not embedded" — the
                              dev verifies via SSH client prompt only.
    """
    if cli not in VALID_CLIS:
        raise ValueError(f"cli must be one of {VALID_CLIS!r}, got {cli!r}")
    if platform not in VALID_PLATFORMS:
        raise ValueError(
            f"platform must be one of {VALID_PLATFORMS!r}, got {platform!r}"
        )
    if agent_app not in VALID_AGENT_APPS:
        raise ValueError(
            f"agent_app must be one of {VALID_AGENT_APPS!r}, got {agent_app!r}"
        )

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
        agent_app=agent_app,
        agent_app_display=_AGENT_APP_DISPLAY_NAMES[agent_app],
        platform=platform,
        mcp_config_path=_MCP_CONFIG_PATHS[cli],
        expires_iso=expires_at.isoformat(),
        expires_human=expires_at.strftime("%Y-%m-%d %H:%M %Z").strip(),
        bootstrap_expiry_iso=bootstrap_expiry.isoformat(),
        bootstrap_expiry_human=bootstrap_expiry.strftime("%Y-%m-%d %H:%M %Z").strip(),
        bootstrap_private_key=bootstrap_private_key,
        ssh_host=ssh_host,
        ssh_port=ssh_port,
        ssh_host_fingerprint=ssh_host_fingerprint or "(verify on first SSH connect)",
    )

    sections: list[str] = [
        _PREAMBLE,
        _PHASE0_TRUST,
        _phase_block(_PHASE1_HEADER, _PHASE1_BASH, _PHASE1_POWERSHELL, platform),
        _phase_block(_PHASE2_HEADER, _PHASE2_BASH, _PHASE2_POWERSHELL, platform),
        _phase_block(_PHASE3_HEADER, _PHASE3_BASH, _PHASE3_POWERSHELL, platform),
        _phase_block(_PHASE4_HEADER, _PHASE4_BASH, _PHASE4_POWERSHELL, platform),
        _PHASE4_FOOTER,
        _PHASE5_BY_CLI[cli],
        _PHASE5_COMMAND,
        _phase_block(_PHASE6_HEADER, _PHASE6_BASH, _PHASE6_POWERSHELL, platform),
        _PHASE7_CLEANUP,
        _BOOTSTRAP_KEY_BLOCK,
    ]
    content = "".join(s.format(**fmt_args) for s in sections)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o600)
    return path
