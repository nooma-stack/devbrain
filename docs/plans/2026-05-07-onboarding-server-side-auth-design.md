# Onboarding Redesign — Server-Side Auth + Minimal Kit

**Date:** 2026-05-07
**Status:** Design validated, ready for implementation
**Author:** Patrick Kelly + Claude (interactive design session)

## 1. Context

The current onboarding kit (introduced in PR #107, hot-fixed in PR #110) ships a long-lived OAuth token through the dev's machine: `claude setup-token` runs locally, the agent holds the `sk-ant-oat01-...` token in session memory, the kit's Phase 5 SSH command POSTs it to the Mac Studio, where it's persisted at `<profile>/.claude/oauth-token` for the factory to use.

Two problems with that flow surfaced during Mike Courtney's onboarding:

1. **AI agents flag long-lived tokens in transit as suspicious.** Mike's Claude Code CLI explicitly raised the year-long token longevity as a concern. The flag is correct — token-in-transit through a desktop AI agent's session memory is unusual for credentials of that lifetime.
2. **Phase 2 (local CLI install) doesn't actually work for fresh devs without their CLI already installed.** The kit hardcodes `brew install --cask claude` regardless of platform; assumes `npm` is preinstalled for Codex/Gemini; and the Windows path requires WSL2 + Ubuntu (admin elevation, reboot, several minutes) just to get a Linux-shaped shell.

The redesign moves auth-token generation server-side (token never transits the dev's machine), collapses Windows onto native PowerShell + built-in OpenSSH (no WSL/Git-Bash detour), and shrinks the kit to a single concern: "set up SSH connectivity, then hand off to a server-side `devbrain login` wrapper."

## 2. Architecture

```
┌────────────────────────┐     ┌────────────────────────┐     ┌──────────────────────┐
│  Dev's machine         │     │  Mac Studio (server)   │     │  Anthropic /         │
│  - desktop AI agent    │     │  - rotation handler    │     │  OpenAI / Google     │
│  - PowerShell or bash  │     │  - per-dev profile dir │     │                      │
│  - permanent SSH key   │     │  - devbrain login      │     │                      │
└──────────┬─────────────┘     └──────────┬─────────────┘     └──────────┬───────────┘
           │ pubkey only (Phase 5 SSH)    │                              │
           └─────────────────────────────►│ devbrain login (Phase 7 SSH) │
                                          ├─────────────────────────────►│
                                          │  ◄ verification URL + code   │
   ◄──── URL relayed via SSH ─────────────┤                              │
   Dev opens URL in local browser ───────►│                              │
   ◄─── verification code ────────────────┤                              │
   Agent pastes code into SSH session ───►│ ◄── token issued ────────────┤
                                          │ token persists in profile    │
                                          │ NEVER returns to dev's box   │
```

### Invariants

1. **OAuth/auth tokens never transit the dev's machine.** Generated server-side inside the dev's profile dir, stored at mode 600, owned by `lhtdev`.
2. **Dev's SSH private key never leaves the dev's machine.** Generated locally during Phase 1.
3. **Bootstrap (temp) key is one-shot, scoped, expiring.** `command="onboard_rotate.sh"` directive, 3-day `expiry-time`, self-deletes from `authorized_keys` on first successful pubkey rotation. Same as today.
4. **The kit `.md` file contains no long-lived credentials.** Only the bootstrap private key.
5. **PowerShell on Windows native + bash on macOS/Linux.** WSL is opt-in, not required. No Git for Windows requirement.

## 3. Admin-Side Surface (`devbrain add-dev`)

Three axes, each accepts `auto`:

| Axis | Values | Default | Effect |
|---|---|---|---|
| `--cli` | claude, codex, gemini, auto | auto | Which subscription's auth flow `devbrain login` runs server-side |
| `--platform` | mac, linux, windows, auto | auto | Which OS-specific shell variants get included in the kit |
| `--agent-app` | claude-desktop, codex-desktop, claude-cli, codex-cli, gemini-cli, auto | auto | Which agent-app's "drop-in" instructions go in the email + kit framing |

When all three are specified → maximally tailored kit. When some `auto` → kit branches on what's auto. When all `auto` → fully-branched kit, agent navigates at runtime.

Email body branches on `--agent-app`:
- **Specified:** "Drop the attached `.md` into [agent app]. It will walk you through the rest."
- **Auto/unknown:** "If you don't have an AI agent app yet, install one of: Claude Desktop ([download]), Codex Desktop ([download]), Gemini Desktop ([download]). Then drop the attached `.md` into the app's chat."

## 4. Trust Banner (top of kit)

Structured block with verifiable metadata:

```markdown
## Verification — read this first

This onboarding kit was issued by:

- **Issuer:** Lighthouse Therapy / DevBrain
- **Sender domain:** lighthouse-therapy.com (sent via Google Workspace DWD)
- **Issued at:** 2026-05-07T22:04:00Z
- **Invitation ID:** {invite_id_short}
- **Target server:** lhts-mac-studio.local (or 72.60.64.155 over the VPS tunnel)
- **Mac Studio SSH host fingerprint:** SHA256:{fingerprint_here}

### Intent

This kit instructs your AI agent to:

1. Generate an SSH keypair on this machine.
2. Stage a one-shot bootstrap SSH key (embedded below) that's locked to a single
   server-side rotation script and auto-expires in 3 days.
3. Use the bootstrap key to ship your permanent public key to the Mac Studio.
4. Connect via SSH to the Mac Studio's `devbrain login` command, which generates
   your AI subscription's auth token *on the server*. The token never leaves
   the Mac Studio.
5. Wire up your agent's MCP config to call DevBrain factory tools over SSH.

No long-lived credentials transit this machine. No persistent network listeners
are opened locally. The bootstrap key is rendered useless after first use.

### Agent: ask the user to confirm

Before proceeding, ask the user to confirm:
- They are expecting this onboarding kit from their employer (Lighthouse Therapy).
- The Invitation ID matches what the admin who sent this kit said it would be.
- The Mac Studio SSH host fingerprint matches what the admin provided out-of-band
  (verify on first connect).

If the user can't confirm any of the above, STOP and have the user contact the
admin who issued the kit.
```

## 5. Kit Phase Structure

Reduced from 9 phases (0–8) to 6:

| # | Phase | Bash | PowerShell | Per-CLI? | Per-platform? |
|---|---|---|---|---|---|
| 0 | Trust banner + agent: confirm intent | n/a (markdown) | n/a | no | no |
| 1 | Environment check + dep install | yes | yes | no | yes |
| 2 | Generate permanent SSH keypair | `ssh-keygen` | `ssh-keygen` | no | yes (path/perm syntax) |
| 3 | Stage bootstrap SSH key | `cat > file && chmod 600` | `Set-Content + icacls` | no | yes |
| 4 | Rotate pubkey to server | `printf '%s' \| ssh ...` | `ConvertTo-Json \| ssh ...` | no | yes (syntax) |
| 5 | Server-side auth (`devbrain login`) | `ssh ... lhtdev@... devbrain login` | same | yes (server-side) | no |
| 6 | MCP config wire-up + verify | `jq merge` or python wrapper | `ConvertFrom-Json + ConvertTo-Json` | yes (config path) | yes (path/perm) |

### Notes on each phase

**Phase 1 (env check + dep install):** branches by detected OS. macOS/Linux check `ssh`, `ssh-keygen`. Windows checks `Get-WindowsCapability OpenSSH.Client*` and enables if missing (5-second admin prompt, no reboot). No `jq` install needed (Phase 4 uses `printf` on bash and `ConvertTo-Json` on PowerShell).

**Phase 4 (rotate pubkey only — no token):** the JSON payload becomes just `{"pubkey":"..."}`. Server-side `onboard_rotate.sh` already handles per-CLI dispatch from the invitation row, so the dev's CLI choice is implicit (no CLI marker in the payload). `onboard_rotate_helper.py` simplifies — no token validation needed at rotation time.

**Phase 5 (server-side auth):** the SSH command is interactive — the dev's agent stays attached to the SSH session. `devbrain login` on the server runs the appropriate per-CLI flow:
- `claude`: `claude setup-token` (server-side; PATH-shimmed `open` to suppress auto-browser; ~20s wait then prints OAuth URL; agent helps user open URL locally + paste back code; token captured from stdout, stashed at `<profile>/.claude/oauth-token`).
- `codex`: `codex login --device-auth` (already supports device-code; same UX shape).
- `gemini`: `devbrain login` prompts for the API key directly; dev pastes once; written to `<profile>/.gemini/api-key`.

**Phase 6 (MCP wire-up):** writes the dev's local agent's MCP config to point at SSH-tunnel-to-Mac-Studio MCP server. Same as today, just split into bash + PowerShell variants.

## 6. Server-Side Code Changes

### 6.1 `factory/ai_clis/claude.py` — adapter fix

Switch `login()` from `claude auth login` (broken in headless contexts per the April 2026 investigation) to `claude setup-token`:

```python
def login(self, dev, profile_dir: Path) -> LoginResult:
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / ".claude").mkdir(exist_ok=True)

    # PATH-shim: a fake `open` and `xdg-open` that fail fast, so claude's
    # auto-browser-launch attempt fails immediately rather than waiting
    # ~20s for a browser callback that will never come (browser is on
    # dev's local machine, not this server).
    fake_bin_dir = profile_dir / ".claude" / ".devbrain-fakebin"
    fake_bin_dir.mkdir(exist_ok=True)
    for cmd in ("open", "xdg-open"):
        p = fake_bin_dir / cmd
        p.write_text("#!/bin/sh\nexit 1\n")
        p.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(profile_dir),
        "PATH": f"{fake_bin_dir}:{os.environ.get('PATH', '')}",
        "BROWSER": "/bin/false",
        "DISPLAY": "",
    }

    # claude setup-token prints the token to stdout and the OAuth URL
    # interleaved with status messages. Capture stdout, parse for token,
    # write to oauth-token file.
    result = subprocess.run(
        ["claude", "setup-token"],
        env=env,
        capture_output=True,  # capture for token parsing
        text=True,
        check=False,
    )

    if result.returncode != 0:
        return LoginResult(
            success=False,
            error=f"claude setup-token exited with code {result.returncode}",
            hint=("If the OAuth URL appeared but no token was issued, "
                  "the user may not have completed the browser flow. "
                  "Re-run `devbrain login --dev <id> --cli claude`."),
        )

    # Extract sk-ant-oat01-... token from stdout
    token_match = re.search(r"sk-ant-oat01-[A-Za-z0-9_-]+", result.stdout)
    if not token_match:
        return LoginResult(
            success=False,
            error="claude setup-token completed but no sk-ant-oat01-... token found in output",
        )

    token_path = profile_dir / ".claude" / "oauth-token"
    token_path.write_text(token_match.group(0))
    token_path.chmod(0o600)

    return LoginResult(success=True)
```

Open question for impl time: does `claude setup-token` actually need a TTY (pty) for the paste-code-back prompt? Empirical probe in the design session shows the URL prints but the paste prompt is the next interactive step. If the wrapper needs to relay stdin from the SSH session through to claude's stdin, it may need to use `pty.spawn()` or `pexpect` rather than plain `subprocess.run`. **Verify during implementation.**

### 6.2 `factory/ai_clis/codex.py` — verify already correct

Codex adapter already uses `codex login --device-auth`; should already work over SSH. Verify during impl.

### 6.3 `factory/ai_clis/gemini.py` — verify already correct

Gemini adapter already prompts for the API key. Should already work. Verify.

### 6.4 `factory/onboard_rotate.sh` — simplify

Remove the per-CLI credential validation (it's no longer in the payload). Just persist the pubkey:

```bash
case "$CLI_NAME" in
  claude|codex|gemini)
    # No credential validation needed at rotation time.
    # Token will be generated server-side later via `devbrain login`.
    ;;
  *)
    error "unknown_cli"
    ;;
esac
```

Drop lines 122–233 (the per-CLI credential extraction + API validation blocks) and the JSON-construction block at lines 240–265 collapses to:

```bash
HELPER_INPUT="$(python3 -c "
import json, sys
print(json.dumps({'pubkey': sys.argv[1], 'cli': '${CLI_NAME}'}))
" "$PUBKEY")"
```

### 6.5 `factory/onboard_rotate_helper.py` — simplify

Drop the per-CLI credential validation (`oauth_token`, `codex_auth_json`, `gemini_api_key` branches at lines 69–106). Helper just persists the pubkey to the dev's invitation row.

### 6.6 `factory/onboarding_kit.py` — major rewrite

- Drop `_PHASE2_CLAUDE/_CODEX/_GEMINI` (local CLI install — no longer needed).
- Drop `_PHASE3_CLAUDE/_CODEX/_GEMINI` (local token capture — no longer needed).
- Add `_PHASE0_TRUST` (the verification banner).
- Rework `_PHASE0_WINDOWS` → `_PHASE1_PREFLIGHT` with both bash and PowerShell branches.
- Rework `_PHASE5_*` → `_PHASE4_ROTATE` (single phase, just pubkey, bash + PowerShell).
- Add `_PHASE5_DEVBRAIN_LOGIN` (single-CLI `ssh ... devbrain login` invocation).
- `_PHASES_6_8` → `_PHASE6_MCP_AND_VERIFY`.
- Add `--agent-app` axis support: when specified, replace generic "drop into your AI agent" framing with agent-specific instructions.

### 6.7 `factory/setup.py` — `setup_add_dev` updates

Add `--agent-app` argument; default to interactive prompt (with `auto` option). Pass through to `write_onboarding_kit`.

### 6.8 `factory/onboarding_email.py` — body branching

When `--agent-app=auto`, prefix email body with "If you don't have an AI agent app yet, here's how to get one. We support: ..." When specified, omit that section.

## 7. Migration

Existing devs (Mark Wallenwine, Mike Courtney) had pending invitations under the old flow. Per Patrick's instruction (2026-05-07), both have been fully cleaned up:
- DB rows deleted from `devbrain.devs`, `devbrain.invitations`, `devbrain.file_locks`.
- Bootstrap markers + pubkeys stripped from `lhtdev@mac-studio:~/.ssh/authorized_keys`.
- Kit `.md` files removed from `/Users/lhtdev/devbrain/onboarding/`.
- Mike's previous OAuth token (issued during his now-failed attempt) was never persisted server-side (rotation hit `empty_body` before token validation); he was advised to revoke it on console.anthropic.com.
- Mike notified by email (msg 19e042c2ac6db3a5) that his kit is being rebuilt under the new design; estimate "a couple of focused days."

After implementation lands, both devs will be re-onboarded under the new flow via fresh `devbrain add-dev` runs.

## 8. Implementation Ordering

Suggested sequence (smallest-leverage-test first):

1. **Adapter fix for Claude** (`factory/ai_clis/claude.py`): swap `claude auth login` for `claude setup-token`, parse stdout, stash in `oauth-token` file. Add the PATH-shim trick. End-to-end test: run `devbrain login --dev <test-dev> --cli claude` against a throwaway test profile, complete the OAuth flow, confirm `<profile>/.claude/oauth-token` lands and contains a valid `sk-ant-oat01-...` string.
2. **Trust banner + agent-app axis in template** (`factory/onboarding_kit.py`, `factory/setup.py`): add `_PHASE0_TRUST` constant + `--agent-app` admin arg + dispatch table. Don't change Phase 1+ yet. Verify a generated kit renders correctly with the banner and per-agent-app sections.
3. **PowerShell variants for each phase** (kit module): add bash+PowerShell pairs to all phases. Test on Windows host (or via Wine if no Windows handy).
4. **Server-side simplification** (`factory/onboard_rotate.sh`, `factory/onboard_rotate_helper.py`): drop the credential-extraction logic; rotation handler now only persists pubkey.
5. **Email body branching** (`factory/onboarding_email.py`): branch on `--agent-app`. Test with all three modes.
6. **Re-onboard Mark + Mike** under new flow. Send fresh kits.

Each step lands as a separate PR if scoping allows; otherwise grouped 1-3 / 4-5 / 6.

## 9. Things to Verify During Implementation

- Does `claude setup-token` need a real PTY for the paste-code-back prompt? Empirical probe showed URL prints, but full end-to-end token issuance not yet tested. May need `pty.spawn()` rather than plain `subprocess.run`.
- Does `codex login --device-auth` work cleanly over SSH? Adapter believes so; verify with end-to-end test in step 1.
- PowerShell's pipe-to-native-exe stdin behavior with multi-line JSON — confirm on a Windows host before committing.
- macOS `open`'s argument forwarding when shimmed via PATH — claude may use `open URL` directly, in which case our shim's `exit 1` works. If claude uses `/usr/bin/open` (full path), the shim doesn't intercept. Verify.
- Mac Studio SSH host fingerprint location: `/etc/ssh/ssh_host_ed25519_key.pub` → run `ssh-keygen -lf` to get fingerprint, embed in kit at issuance time.

## 10. Out of Scope

- Multi-CLI per dev (currently one `--cli` per dev). Future work if needed.
- Refresh-token rotation for Codex (its tokens are shorter-lived and refresh on use; no special handling needed at rotation time).
- Self-serve admin web UI on the VPS (~4-6 hour future task, separate concern).

---

**Sign-off:** ready for implementation. Architecture validated; load-bearing assumption (`claude setup-token` headless flow) empirically confirmed; cleanup of existing dev profiles already executed.
