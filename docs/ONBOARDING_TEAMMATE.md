# Onboarding a Teammate to the Shared DevBrain (Model 1)

This is the playbook for adding a new dev (e.g., Alice) to an existing
**shared DevBrain factory instance** running on a Mac Studio. Devs SSH
into the shared host, work in a persistent tmux session there, and the
factory spawns AI CLIs (claude / codex / gemini) under each dev's own
subscription via per-dev HOME profiles.

For *replacing your own machine* (single-user move), see
[MIGRATING.md](MIGRATING.md). That covers `export-memory` /
`import-memory`. This doc covers the steady-state team scenario.

---

## Architecture in one diagram

```
Alice's MacBook                                        Shared Mac Studio
─────────────────                                      ─────────────────
                          SSH (key-based)
                          (with -R reverse tunnel)
┌─────────────────┐    ────────────────────►       ┌───────────────────┐
│  Chrome +       │                                 │  /Users/lhtdev    │
│  PKRelay        │   ◄──── reverse tunnel ─────    │   ├─ devbrain/    │
│  ext (18793)    │       18794:localhost:18793     │   ├─ profiles/    │
└─────────────────┘                                 │   │   ├─ alice/   │
                                                    │   │   ├─ bob/     │
                                                    │   │   └─ ...      │
                                                    │   └─ tmux session │
                                                    │      'alice'      │
                                                    └───────────────────┘
                                                    (factory orchestrator
                                                     spawns claude/codex/
                                                     gemini under
                                                     ~/devbrain/profiles/
                                                     <dev_id>/ creds)
```

- **One shared Mac Studio user (`lhtdev`)** owns the install. Each dev
  SSHes in with their own key but to the same `lhtdev` account.
- **Per-dev profiles** at `~/devbrain/profiles/<dev_id>/` hold each dev's
  AI CLI credentials, per-dev `.gitconfig`, and symlinks to shared org
  dotfiles (`.npmrc`, gcloud config, etc.).
- **Factory job submitted by Alice** → orchestrator looks up profile →
  spawns `claude` (or codex/gemini) with `HOME=~/devbrain/profiles/alice`
  (or `CODEX_HOME=...` for codex) → runs under Alice's subscription,
  commits as Alice.
- **Browser-driving** for UI work: PKRelay reverse SSH tunnel exposes
  Alice's local Chrome extension to the Mac Studio, so the
  Mac-Studio-side claude session can inspect / interact with her
  browser.

---

## Prerequisites

**Alice's laptop:**
- macOS (primary); Linux works minus a couple of niceties
- An SSH key (`~/.ssh/id_ed25519` or similar)
- PKRelay extension installed in Chrome (see
  [pkrelay/docs/REMOTE-SETUP.md](https://github.com/nooma-stack/pkrelay/blob/main/docs/REMOTE-SETUP.md))
- Her own paid subscriptions: Claude Max, OpenAI/Codex, Google Gemini
  (or API keys if she prefers pay-per-use auth)

**Patrick (host operator) — once per new teammate:**
- Add Alice's SSH pubkey to `lhtdev@mac-studio:~/.ssh/authorized_keys`
- (Optional) Pre-register her in DevBrain so her `.gitconfig` populates
  automatically: `devbrain register --dev-id alice --name "Alice Smith"
  --channel tmux:alice`

---

## Step 1 — Get SSH access to the shared Mac Studio

Alice sends Patrick the contents of her `~/.ssh/id_ed25519.pub`.

Patrick appends it on the Mac Studio:

```bash
ssh mac-studio "echo '<alice-pubkey-line>' >> ~/.ssh/authorized_keys"
```

Then Alice adds an SSH config entry on her laptop:

```sshconfig
# ~/.ssh/config
Host mac-studio
  HostName lhts-mac-studio.local
  User lhtdev
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
  ServerAliveInterval 60
  # PKRelay reverse tunnel — exposes Alice's local browser to the Mac Studio
  RemoteForward 18794 localhost:18793
```

> **Note on OAuth callback ports:** the original design anticipated needing
> `RemoteForward` lines for OAuth callbacks (claude, codex, gemini login
> flows). Behavioral probes confirmed those tunnels are NOT required:
> Codex uses `--device-auth` (no localhost listener), Claude uses a hosted
> callback at `platform.claude.com` (no localhost listener), and Gemini
> can use API-key auth (skips OAuth entirely). Only the PKRelay
> RemoteForward stays.

Verify SSH works:

```bash
ssh mac-studio 'hostname && whoami'
# → LHTs-Mac-Studio.local / lhtdev
```

## Step 2 — Connect & create a persistent tmux session

```bash
ssh mac-studio
tmux new -s alice    # first time
# (later, reattach with: tmux a -t alice)
```

Convention: name the session your `dev_id` (lowercase, alphanumeric +
`-`/`_`, ≤64 chars). The factory's notification system uses this name
to send tmux popups to the right dev.

## Step 3 — Register your DevBrain identity

Inside your tmux session on the Mac Studio:

```bash
devbrain register --dev-id alice \
                  --name "Alice Smith" \
                  --channel tmux:alice
```

Optionally add additional notification channels:

```bash
devbrain register --dev-id alice \
                  --channel tmux:alice \
                  --channel webhook_slack:https://hooks.slack.com/...
```

`register` is idempotent — re-running merges new channels and updates
the full_name. If you skip this step, `devbrain login` will prompt for
your git author identity instead of pulling it from your Dev record.

## Step 4 — Provision your per-CLI logins

```bash
devbrain login --dev alice                # logs in all 3 CLIs
# OR per-CLI:
devbrain login --dev alice --cli claude
devbrain login --dev alice --cli codex
devbrain login --dev alice --cli gemini
```

Each CLI's login flow:

- **Codex**: prints a verification URL + short code. Open the URL in your
  laptop browser, enter the code, return to the SSH terminal. No
  localhost callback (uses `codex login --device-auth`).
- **Claude**: prints a sign-in URL. Open in your laptop browser, complete
  OAuth, the credentials land via Claude's hosted callback at
  `platform.claude.com`. May display a code to paste back into the SSH
  terminal.
- **Gemini**: prompts to choose auth method. If you set
  `dev.gemini_api_key` on your registration, OAuth is skipped and the
  API key is used directly. Otherwise, Google's OAuth flow runs (open
  the printed URL in your laptop browser).

The first time you run `devbrain login`, you'll be prompted for git
author name + email if your Dev record didn't have them. These get
written to `~/devbrain/profiles/alice/.gitconfig` so factory commits
attribute correctly.

`devbrain login` also runs `tmux setenv DEVBRAIN_DEV_ID alice` in your
session, so subsequent `factory submit` calls auto-attribute.

## Step 5 — Verify

```bash
devbrain logins --dev alice
```

Should show:

```
dev_id    claude  codex   gemini
alice     ✅       ✅       ✅
```

Submit a hello-world factory job:

```bash
factory submit "Add a no-op test that asserts True"
```

A tmux popup should appear with the job's progress. The factory will
pick the job up, plan/implement/review/qa using YOUR claude (or codex
or gemini) subscription, and the resulting commit will be authored by
"Alice Smith <alice@…>".

---

## Day-2 operations

### Re-login when an OAuth token expires

Symptom: factory job halts with `needs_human` + a message about OAuth
auth failing. Fix:

```bash
devbrain login --dev alice --cli claude   # or whichever expired
```

The login flow recreates the credentials in your profile. Resume the
halted job with `factory resume <job_id>`.

### Switch between tmux sessions

```bash
tmux ls              # list sessions on the Mac Studio
tmux a -t alice      # attach
tmux switch -t bob   # if you're already attached and want a different one
```

Each session can have a different `DEVBRAIN_DEV_ID` (set automatically
by `devbrain login` when run inside that session).

### Leave the team (offboarding)

Alice's offboarding (Patrick runs):

```bash
# Remove SSH access
ssh mac-studio "sed -i.bak '/alice@/d' ~/.ssh/authorized_keys"

# Wipe the per-dev profile (creds + gitconfig)
ssh mac-studio "cd ~/devbrain && bin/devbrain logout --dev alice --yes"

# Optional: keep her dev row for historical attribution, but clear
# notification channels so she doesn't get pinged
ssh mac-studio 'docker exec devbrain-db psql -U devbrain -d devbrain \
    -c "UPDATE devbrain.devs SET channels='\''[]'\'' WHERE dev_id='\''alice'\'';"'
```

We don't delete the row — it preserves authorship on her past
decisions / sessions / factory jobs.

---

## Troubleshooting

**`devbrain login` exits with `not found` for the CLI binary**
The `claude` / `codex` / `gemini` CLI isn't installed on the Mac Studio.
Patrick installs it via Homebrew or each CLI's official installer.

**OAuth callback hangs / browser shows "connection refused"**
You're hitting the legacy assumption that OAuth needs a tunnel. Check
that you're using `codex login --device-auth` (default in the codex
adapter) — the URL it prints should NOT include `localhost`. If
`devbrain login` is somehow invoking `codex login` (without `--device-auth`),
that's a bug — file an issue.

**Factory job stuck in `needs_human` with "no profile for dev_id"**
Run `devbrain login --dev <id>` to provision the profile, then resume:

```bash
factory resume <job_id>
```

**Factory commit attributed to `lhtdev` instead of you**
Your `.gitconfig` didn't get populated. Check
`~/devbrain/profiles/<dev_id>/.gitconfig` exists and has your
`[user]` block. If missing, re-run `devbrain login` (which populates
gitconfig as a side effect).

**`devbrain logins` shows `❌` for a CLI you logged into**
The `is_logged_in` check looks for credential files in your profile
dir:
- `~/devbrain/profiles/<dev_id>/.codex/auth.json` (codex)
- `~/devbrain/profiles/<dev_id>/.claude.json` (claude)
- `~/devbrain/profiles/<dev_id>/.gemini/google_accounts.json` (gemini)
or `dev.gemini_api_key` set

Check the file actually landed there. If not, the OAuth flow likely
failed silently — re-run `devbrain login` and watch the output.

**PKRelay tab not connecting to your local Chrome**
The reverse tunnel from the Mac Studio back to your laptop's PKRelay
broker isn't up. Verify your SSH config has `RemoteForward 18794
localhost:18793` and that PKRelay is running on your laptop. See
[pkrelay/docs/REMOTE-SETUP.md](https://github.com/nooma-stack/pkrelay/blob/main/docs/REMOTE-SETUP.md).
