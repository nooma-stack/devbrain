# Onboarding redesign: invitation is SSH access; agent self-services per-app auth

> **Status:** Replacement design. Supersedes
> [`2026-05-13-multi-cli-invitation-design.md`](2026-05-13-multi-cli-invitation-design.md)
> (PR #131), which was overengineered for the actual problem.
>
> **Scope:** Decouple infrastructure access from per-app auth. One
> invitation gets a dev's agent SSH'd in; the agent then runs whatever
> per-app auth flows it needs (claude, codex, gemini, future apps)
> without admin involvement.
>
> **Verification gate:** Mike's existing onboarded row remains usable
> with no migration footprint beyond informational column changes. A
> new dev onboarding to two apps from one machine requires one
> invitation, one kit, one SSH session.

---

## 1. Why this redesign

PR #131 set out to solve a real pain (Mike's claude-desktop onboarding
required a second invitation after his earlier claude-code attempt),
but it conflated two concerns that should be separate:

| Concern | Whose job | Tracked where |
|---|---|---|
| Infrastructure access (SSH key, dev identity, machine identity) | **Admin** | `devbrain.invitations` table |
| Per-app auth (OAuth tokens, API keys, per-CLI config) | **Dev's agent** | Dev's profile directory on disk |

PR #131 tried to make the admin orchestrate per-app auth state through
a `cli_targets` jsonb column, per-target status objects, partial-state
machine, kit-renders-N-flows complexity, and bootstrap-key lifecycle
gymnastics. Almost all of that disappears once the model is:

> **Invitation = SSH access to a machine. Once the dev's agent is in,
> the agent handles its own per-app auth. Adding a new app later is
> the agent's job, not the admin's.**

---

## 2. Locked decisions

| # | Decision |
|---|---|
| 1 | An invitation onboards **one (dev, machine)** pair. Re-using the same dev on a second machine = a second invitation. |
| 2 | Invitations no longer carry a `cli` field as load-bearing state. The existing scalar `invitations.cli` column degrades to **informational** (recorded for context; no enforcement). |
| 3 | The kit emits a single SSH bootstrap flow that's cli-agnostic. After SSH, the dev's agent is responsible for whatever app auth is needed. |
| 4 | Per-app auth runs via `devbrain login --cli=<app>` (existing CLI command). The agent invokes it for each app the dev wants set up. |
| 5 | Per-app credentials land in the dev's profile directory at `<DEVBRAIN_HOME>/profiles/<dev_id>/{.claude,.codex,.gemini}/`. Profile dir is already Phase 2 territory. |
| 6 | Bootstrap key keeps its current behavior: short TTL, self-deletes on first rotation. The first rotation appends the dev's permanent SSH key, and all subsequent per-app auth flows run through the permanent key. |
| 7 | Adding a new agent app (claude-desktop, future CLI X) = author a new `devbrain login --cli=X` recipe. **No invitation/kit change.** |
| 8 | "Which apps does a dev have set up?" is observable from the on-disk profile (e.g. presence of `.claude/oauth-token`), surfaced via a new `devbrain logins` query that already exists. |

---

## 3. What this preserves

- `devbrain.invitations` table shape (no jsonb refactor needed)
- Bootstrap SSH key + permanent key rotation flow (already works for Mike)
- `reconciler.py` activation logic (appends pubkey, marks `status='activated'`)
- Per-dev profile dirs from Phase 2 (`factory/profiles.py`)
- `factory/ai_clis/{claude,codex,gemini}.py` adapter pattern from PR #51
- `devbrain login` CLI command (already exists, per `factory/cli.py`)

## 4. What this removes from PR #131's scope

- ❌ `cli_targets jsonb` column + GIN index
- ❌ Top-level `partial` status value + rollup function
- ❌ Per-target status objects (`{cli, agent_app, status, pubkey_received_at, oauth_token_received_at, oauth_token, failed_reason}`)
- ❌ Kit rendering N flow blocks per target
- ❌ Bootstrap key lifecycle options (a/b/c from PR #131 §10)
- ❌ `agent_app` enum
- ❌ Per-target expiration logic
- ❌ Mike's row migration / agent_app backfill

That entire complexity surface was the symptom of mixing the two
concerns. Separating them dissolves it.

---

## 5. Mike's worked example under the new model

```
Day 1 — admin onboards Mike:
  devbrain invite --dev=mike_courtney --machine=mikes-mbp
  → emails Mike his kit (one cli-agnostic SSH flow)

Day 1 — Mike opens the kit, drops it into his agent (claude-code):
  agent SSHes into Mac Studio with bootstrap key
  agent runs onboard_rotate.sh: appends Mike's permanent pubkey to
    authorized_keys, marks invitation row status='activated'
  agent self-services claude-code auth:
    devbrain login --cli=claude
    (runs claude setup-token, writes to ~/profiles/mike_courtney/.claude/)
  agent confirms: "you're all set for claude-code"

Day 7 — Mike says "I want claude-desktop too":
  agent SSHes in with permanent key (already works)
  agent runs: devbrain login --cli=claude --agent-app=claude-desktop
    (runs the appropriate per-app flow; could be browser-handoff for
     Desktop or setup-token for CLI)
  agent confirms: "claude-desktop is set up"
  No new invitation, no admin involvement.

Day 30 — Mike says "I want codex too":
  agent SSHes in with permanent key
  agent runs: devbrain login --cli=codex
  Done.

Day 90 — Mike gets a second machine (a Windows desktop):
  Admin runs: devbrain invite --dev=mike_courtney --machine=mikes-windows
  → second invitation, second SSH onboarding flow, but same dev_id.
  Mike's agent on the Windows box self-services its own per-app auth.
```

---

## 6. `devbrain login` per-app contract

The `devbrain login --cli=<app>` command is the contract surface
between "agent post-SSH onboarding" and "per-app auth". For each
supported `(cli, agent_app)` pair, it:

1. Detects whether the app is installed on the host (best-effort —
   exits cleanly with a helpful message if not).
2. Runs the app's installer/auth flow non-interactively where
   possible, or surfaces a clear interactive handoff (e.g. "open this
   URL in your browser and paste the code back").
3. Writes the resulting credential to the dev's profile dir.
4. Updates a per-dev manifest (`<profile>/installed.json` or similar)
   so `devbrain logins` can report.
5. Is idempotent — re-running for an already-configured app is a
   no-op (or refreshes the credential if expired).

Supported apps at v1 (matches `factory/ai_clis/`):

- `--cli=claude` `--agent-app=claude-code` (default for `claude`)
- `--cli=claude` `--agent-app=claude-desktop`
- `--cli=codex`
- `--cli=gemini`

New apps land as new entries here. No invitation-table changes
required for any of them.

---

## 7. Schema delta

Effectively zero. The only consideration is whether to keep
`invitations.cli` for backwards compat or drop it.

**Proposal:** keep it for one release as informational (the kit
originally requested for this cli/app), then drop in a follow-up
migration once any external consumers (dashboard, audit) have
adapted. No new columns, no jsonb, no per-target tracking.

---

## 8. Implementation phasing

Most of the substrate already exists. Remaining work is small:

- **Phase A:** make the kit cli-agnostic. The current kit template
  embeds claude-specific flow blocks; replace with a single SSH
  bootstrap flow that ends with "ask your agent to run
  `devbrain login --cli=<whatever_you_want>`."
- **Phase B:** audit `devbrain login` coverage. Verify it works
  end-to-end for `claude`, `claude-desktop`, `codex`, `gemini`. Fill
  in gaps. Issue #126 (claude.py oauth-token DB update) is one such
  gap and lands here.
- **Phase C:** docs + onboarding-teammate.md update. Spell out the
  new contract so the next external dev onboarding (post-Mike) goes
  through the simpler path.
- **Phase D:** observability. `devbrain logins <dev_id>` reads the
  per-dev profile manifest and reports which apps are installed.

Optional later:

- **Phase E:** deprecate `invitations.cli` column. Drop in a future
  migration once unused.

---

## 9. Roll-back

Trivial: this redesign mostly *removes* state machinery rather than
adding it. The kit template change is the only user-visible diff,
and it gracefully degrades — an old single-CLI kit still works if a
dev happens to have one in flight when the new template lands. No
schema migration to undo.

---

## 10. Open decisions

Genuinely small set after the simplification:

1. **Should `devbrain invite` accept an optional `--cli` hint?**
   Useful for kit messaging ("you wanted claude-code") but not
   load-bearing. Recommend: yes, as a free-text annotation, no
   enum, no enforcement.

   **RESOLVED (2026-05-19)** — `devbrain invite <dev_id>` shipped as
   a non-interactive top-level command with `--cli` accepted as a
   free-text annotation (default `"claude"`). Enforcement stays out
   of the column; the kit/email body uses the hint for phrasing only.
   See `factory/cli.py:invite_cmd` and
   `factory/setup.py:finalize_invitation_and_kit`.

2. **Browser-required auth flows in headless mode.** Some agent apps
   (claude-desktop) require a browser. The agent might not have one.
   Recommend: surface a clear interactive handoff to the human dev
   ("open this URL on your laptop, paste the code back"). Don't try
   to automate browsers from the dev's agent.

   **RESOLVED (2026-05-19)** — All three supported adapters
   (`claude.py` / `codex.py` / `gemini.py`) already block auto-launch
   (`BROWSER=/bin/false`, `DISPLAY=""`) and print a URL for the dev
   to open in their laptop browser. Claude Desktop is explicitly
   NOT supported as an SSH-tunnel target — it's a local GUI app and
   devs install it directly on their own machine. Documented in
   `docs/ONBOARDING_TEAMMATE.md` §"Browser-required auth from a
   headless SSH session".

Both are minor compared to PR #131's 6 open questions, all of which
dissolve here.

---

## 11. Decision: close PR #131

PR #131 should be closed as superseded, not merged. Its design doc
remains in the docs tree for historical context but the implementation
plan it proposed should not be carried out. This document is the
replacement.
