# First BrightBot Dev Onboarding — Runbook + Smoke Test Plan

> **Status:** Runbook for executing the first real BrightBot dev onboarding using
> already-shipped infrastructure. NOT a design doc — the architecture was
> finalized in PRs #51-66 (multi-dev) + the Tahoe TCC fix (PR #78) + the
> `devbrain setup add-dev` flow.
>
> **Scope:** Onboard one real BrightBot dev (other than Patrick) end-to-end.
> Smoke-test the full multi-dev factory pipeline against a real BrightBot
> work item.
>
> **Verification gate:** new dev's first factory job in the BrightBot project
> runs cleanly QUEUED → PLANNING → IMPLEMENTING → REVIEWING → READY_FOR_APPROVAL
> with the curator brief surfacing the 5 seeded compliance rules (HIPAA + SOC2
> + FERPA, per PR #91), eval_security + eval_test agents firing in REVIEWING,
> and per-dev attribution visible in `devbrain.factory_jobs.submitted_by`.

---

## 1. The architecture (already shipped — no design changes)

**Credential isolation:** each dev brings their own long-lived Claude OAuth
token via `claude setup-token` on their own `claude.com` account. The token
is stashed per-dev under `lhtdev`'s home on Mac Studio (mode 0600), read by
the Claude CLI adapter at spawn time, never touched via macOS keychain.
Per-dev billing tied to per-dev Max or API subscription.

**No HOME-swap of credentials.** The earlier PR-#51-66 design (HOME-swap
isolating OAuth tokens) was abandoned after the 2026-04-30 keychain finding.
HOME-swap still applies for non-credential state (project history, MCP
cache, gitconfig).

**Onboarding flow:**

| Phase | Action | Who | Where |
|---|---|---|---|
| 1 | `devbrain setup add-dev` — collect dev_id, name, email, channels; stage row in `devbrain.devs`; create invitation row with temp ed25519 key (3-day TTL) + invite token; write Markdown kit | Admin (Patrick) | Mac Studio console |
| 2 | Send kit to dev via gmail_dwd channel OR hand-deliver | Admin | Mac Studio console |
| 3 | Dev pastes kit into their AI agent (Claude Code / Codex / etc.); agent walks Phases 1-5 of kit | Dev or Dev's AI | Dev's machine |
| 4 | Dev `brew install --cask claude`; `claude /login` (browser OAuth); `claude setup-token` produces long-lived OAuth token | Dev | Dev's machine |
| 5 | Agent SSHes into Mac Studio with embedded temp key, single-shot rotation: submits permanent pubkey + OAuth token via JSON | Dev's agent | Mac Studio (over SSH) |
| 6 | Server-side `onboard_rotate.sh` validates OAuth against api.anthropic.com (two-factor check), persists pubkey to `authorized_keys`, stashes OAuth token, signals reconciler | Mac Studio | Mac Studio |
| 7 | `onboard_reconciler.py` (cron or on-demand) marks invitation `ready` → activates dev, populates per-profile `.claude/` settings, emits notifications | Reconciler | Mac Studio |
| 8 | Dev SSHes in with their permanent key; `factory submit "<spec>" --dev <dev_id>` sends a real factory job | Dev | Dev's machine → Mac Studio |
| 9 | Job runs through full pipeline; admin observes in dashboard | Mac Studio | factory dashboard |

## 2. Pre-flight checks (do these before sending the kit)

Each must be ✅ before kit goes out:

| # | Check | Command | Pass criteria |
|---|---|---|---|
| 1 | Mac Studio reachable on its public-facing addr | `ssh mac-studio 'echo ok'` from MacBook | Returns `ok` |
| 2 | DevBrain installed at HEAD (post-PR #91) | `ssh mac-studio 'cd ~/devbrain && git log -1 --format=%H'` | Returns `c1c1dbb` or later |
| 3 | DevBrain DB is healthy | `ssh mac-studio 'cd ~/devbrain && devbrain devdoctor'` | All checks pass |
| 4 | gmail_dwd channel works | `ssh mac-studio 'cd ~/devbrain && devbrain notify --channel=gmail_dwd --to=<admin> --subject=test --body=test'` | Email lands |
| 5 | `onboard_reconciler.py` is running (cron / launchd) | `ssh mac-studio 'launchctl list | grep onboard'` | Reconciler in list |
| 6 | BrightBot project exists in `devbrain.projects` and has compliance_profiles_enabled set | `ssh mac-studio 'PGPASSWORD=... psql -h 127.0.0.1 -p 5433 -U devbrain -d devbrain -c "SELECT slug, compliance_profiles_enabled FROM devbrain.projects WHERE slug=brightbot"'` | Row present with `{hipaa,soc2,ferpa}` |
| 7 | Claude CLI installed at recent version on Mac Studio | `ssh mac-studio 'claude --version'` | ≥2.1.x |
| 8 | First eval_security + eval_test invocations test green from CLI | `ssh mac-studio 'cd ~/devbrain && devbrain test --target=curator-eval'` | Tests pass |

If any check fails, fix before sending kit. Don't onboard into broken
infrastructure.

## 3. Choosing the first dev

**Criteria for the inaugural onboarding:**

- Familiar with Claude Code / Codex CLI workflow (so they can paste the kit
  into an agent and have the agent drive the rotation flow).
- Has a Claude.com account (Max or API; API is fine for inaugural test).
- Available for ~30-60 min interactive support window (they'll likely hit
  a question; we want to be on call).
- BrightBot work to do (so the inaugural factory job is real, not synthetic).

**Recommendation for inaugural:** patrick@lighthouse-therapy.com onboarding
*himself* as a separate dev (`patrick-secondary` or similar) — this lets the
flow be exercised end-to-end with maximum control. The patrick-test profile
that was created in the failed Phase 6 smoke test (2026-04-30) is leftover
and should be archived first.

After the patrick-as-second-dev path is validated, the next real dev gets the
kit. Order matters — don't burn the first real dev's confidence on a flow
that hasn't been smoke-tested by you.

## 4. The smoke test (what "done" looks like)

After Phase 7 of the kit completes (reconciler activates the dev), run this
sequence on the Mac Studio:

```bash
# 1. Verify the dev row activated
psql -c "SELECT id, status, activated_at FROM devbrain.devs WHERE id='<dev-id>'"
# expect: status='active', activated_at NOT NULL

# 2. Verify the per-profile .claude exists
ls -la /Users/lhtdev/devbrain/profiles/<dev-id>/.claude
# expect: settings.json + CLAUDE.md (per-dev personalization)

# 3. Verify their OAuth token is stashed and readable
ls -l /Users/lhtdev/devbrain/profiles/<dev-id>/.devbrain/oauth-token
# expect: mode 0600, owner lhtdev

# 4. Submit a test factory job AS the new dev
factory submit \
  --project=brightbot \
  --dev=<dev-id> \
  --spec="Add a docstring to brightbot/app/services/whatever.py:funcname"

# 5. Watch the dashboard until job lands at ready_for_approval
factory dashboard

# 6. Verify the curator brief generated for this job surfaced the 5 seeded compliance rules
psql -c "SELECT curator_brief->'rules' FROM devbrain.factory_jobs WHERE submitted_by='<dev-id>' ORDER BY created_at DESC LIMIT 1"
# expect: array with 5 entries (HIPAA, SOC2, FERPA mix)

# 7. Verify eval_security + eval_test fired
psql -c "SELECT artifact_type, COUNT(*) FROM devbrain.factory_artifacts WHERE job_id=(SELECT id FROM devbrain.factory_jobs WHERE submitted_by='<dev-id>' ORDER BY created_at DESC LIMIT 1) GROUP BY artifact_type"
# expect: eval_security and eval_test rows present

# 8. Verify per-dev attribution is preserved in git authorship
ssh mac-studio 'cd /tmp/factory-job-<id> && git log -1 --format="%an <%ae>"'
# expect: dev's name + email, NOT lhtdev's
```

If all 8 checks pass, **the first BrightBot dev is onboarded**.
Mark Phase 6 smoke test complete; close the 2026-04-30 keychain issue with
a fix-applied note pointing at the per-dev OAuth token approach.

## 5. Failure modes + fallbacks

| Failure | Likely cause | Fallback |
|---|---|---|
| Reconciler doesn't fire after rotation | Reconciler launchd not running OR DB pool exhausted | Run reconciler manually: `cd ~/devbrain && devbrain reconcile-onboarding` |
| OAuth validation fails ("invalid token") | Dev pasted a Console API key instead of a setup-token output, or token expired | Check `claude api me --api-key=$TOKEN` exit code; if 401, re-issue invitation, dev runs `claude setup-token` again |
| `claude setup-token` not available in dev's CLI | Dev's CLI is older than ~2.0.x | Have dev `npm i -g @anthropic-ai/claude-code@latest` then retry |
| Factory job hangs at PLANNING | Per-dev OAuth token validation succeeded but spawn-time auth fails | Check `factory/cli_executor.py` logs; verify `ANTHROPIC_API_KEY` env propagation |
| Curator brief generates with 0 rules | BrightBot project's `compliance_profiles_enabled` not set OR PR #91's cross-project rule fix not deployed | Check pre-flight #6; confirm `git log -1` shows ≥c1c1dbb |
| Per-dev attribution missing in git authorship | `GIT_CONFIG_GLOBAL` env var not propagated to claude subprocess | Check `factory/ai_clis/claude.py` env shape; verify `git config --global --list` inside spawned subprocess |
| Email channel fails (kit doesn't deliver) | gmail_dwd creds rotated OR scopes missing | Run pre-flight #4; if fails, check `gcloud auth print-access-token` against the brightbot-service SA |

## 6. Post-onboarding follow-ups

After the inaugural onboarding lands successfully:

1. **Archive the test rows** from the 2026-04-30 failed smoke test:
   `DELETE FROM devbrain.factory_jobs WHERE submitted_by='patrickkelly-test' AND archived_at IS NULL` (after backup).
2. **Update WORK_LOG** with completion note.
3. **Store DevBrain milestone** — `mcp__devbrain__store` with type=decision documenting the first BrightBot onboarding.
4. **Send the second dev** their kit using the now-validated flow.
5. **Start scheduling cognify passes** (Phase 6) since the cadence + dev volume now matters.

## 7. What this runbook does NOT cover

- **Public-facing webhook deploy** for the rotation handler (DNS, Traefik,
  reverse SSH tunnel for inbound HTTP). The handoff calls this out:
  > "macOS Tahoe TCC fix is already shipped (PR #78); no public deploy
  >  plumbing but not blocking primary onboarding which uses SSH temp-key"
  Primary onboarding uses SSH inbound, which works today. Public webhook
  is a future convenience for browser-based rotation; not a blocker.
- **Multi-dev concurrent factory job execution** (rate-limit interaction
  when 2 devs hit the same Max subscription via shared `claude` runtime).
  Inaugural is one dev; concurrency surfaces only at dev count ≥2.
- **Cross-project dev access controls** (this dev can submit jobs to
  brightbot but not to lht-vps). Today every active dev can submit to any
  project; per-project ACLs are Phase 7 ops scope.
- **Dev offboarding flow** — deactivating a dev who leaves. Likely just
  sets `status='inactive'`; not yet exercised. Out of scope.

## 8. References

- DevBrain decision `9287ab95` — Atlas Phase 3 complete (substrate ready)
- DevBrain decision `00b295f0` — canonical 'devbrain' rules library (PR #91)
- DevBrain issue (open at 2026-04-30) — Multi-dev profile routing keychain failure
  — **needs to be closed-resolved after this onboarding succeeds**, with the
  per-dev OAuth token resolution noted
- `factory/onboarding_kit.py` — kit generator
- `factory/onboard_rotate.sh` — server-side rotation handler
- `factory/onboard_reconciler.py` — async activation
- `factory/notifications/channels/gmail_dwd.py` — kit email channel
- `docs/plans/2026-04-28-multi-dev-impl-plan.md` — Phase 1-5 multi-dev impl plan (the HOME-swap-only era)

## 9. Out of scope (for future runbooks)

- Bulk onboarding (5+ devs at once)
- Off-network developer (no Mac Studio SSH access; would need public webhook plumbing)
- Self-service dev onboarding via web UI (Phase 7+ opportunity)
- Audit log + admin notification when a dev's OAuth token approaches expiry
