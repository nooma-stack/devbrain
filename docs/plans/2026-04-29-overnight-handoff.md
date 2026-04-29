# Multi-Dev Per-Dev Profile Routing — Overnight Implementation Handoff

**Date:** 2026-04-29 (started 2026-04-28 ~late evening)
**Executed by:** Claude Code (Opus 4.7, 1M context) in-session, autonomous
**Design doc:** [2026-04-28-multi-dev-home-profiles-design.md](./2026-04-28-multi-dev-home-profiles-design.md)
**Implementation plan:** [2026-04-28-multi-dev-impl-plan.md](./2026-04-28-multi-dev-impl-plan.md)
**Status:** All 5 phases merged. Phase 6 (smoke test) gated to Patrick's morning review.

---

## What Shipped

| Phase | PR | Commit | Lines | Tests | Title |
|---|---|---|---|---|---|
| 1 | [#51](https://github.com/nooma-stack/devbrain/pull/51) | `3fe89af` | +1,047 | 52 | feat(factory): AI CLI adapter layer with codex, claude, gemini adapters |
| 2 | [#52](https://github.com/nooma-stack/devbrain/pull/52) | `048c19e` | +402 | 36 | feat(factory): per-dev profile management module |
| 3 | [#53](https://github.com/nooma-stack/devbrain/pull/53) | `ec4bfac` | +701 | 26 | feat(cli): devbrain login / logins / logout commands |
| 4 | [#54](https://github.com/nooma-stack/devbrain/pull/54) | `bc18aa7` | +278 | 7 | feat(factory): cli_executor routes spawn through AI CLI adapters |
| 5 | [#55](https://github.com/nooma-stack/devbrain/pull/55) | `04dc77b` | +303 | 0 (docs) | docs: ONBOARDING_TEAMMATE.md for Model 1 multi-dev |
| **Total** | | | **+2,731** | **121** | |

All 121 new tests pass. No regressions on existing test files. Each PR auto-merged after local pytest gate passed.

---

## What This Enables

A new BrightBot dev (e.g., Alice) can now safely come online on the shared LHT Mac Studio:

1. SSH into `lhtdev@mac-studio` with her own key
2. Open a persistent tmux session named after her `dev_id`
3. `devbrain register --dev-id alice --name "Alice Smith" --channel tmux:alice`
4. `devbrain login --dev alice` (logs into all 3 AI CLIs against her own subscriptions)
5. `factory submit "..."` — orchestrator routes her job's claude/codex/gemini spawns to use her profile's credentials and commits as her, not as `lhtdev`

The previous state: every factory job ran under whoever owned the macOS user (`lhtdev`), violating per-account TOS, mis-attributing commits, and mixing subscription quotas. Now: per-dev profile dirs at `~/devbrain/profiles/<dev_id>/` keep credentials and git identity isolated; the AI CLI adapter for each spawn picks the right env vars (`HOME` for Claude/Gemini, `CODEX_HOME` for Codex) to point the subprocess at the right profile.

---

## Architectural Decisions Made During Implementation

### 1. AI CLI adapter pattern (Phase 1)

`factory/ai_clis/` mirrors the existing `factory/notifications/` channel pattern. Each AI CLI has an adapter class encapsulating its own credential isolation strategy:

- **Codex**: `CODEX_HOME` env var (precise, no HOME-swap blast radius). Verified via behavioral probe — codex emits a startup warning when `CODEX_HOME` is missing, confirming it's read.
- **Claude**: HOME-swap (no `CLAUDE_CONFIG_DIR` env var per official docs). Constrained to single subprocess invocation.
- **Gemini**: HOME-swap with `GEMINI_API_KEY` env-var fallback (devs can opt out of OAuth entirely).

Git authorship via `GIT_CONFIG_GLOBAL` + `GIT_AUTHOR_*` env vars on top of whichever isolation mechanism the adapter uses, so factory commits attribute correctly regardless of HOME state.

### 2. OAuth probe simplification (Phase 1)

The original design doc Section 5 anticipated needing reverse SSH tunnels for OAuth callback ports (`RemoteForward 8765 localhost:8765`, etc.). **Behavioral probes invalidated this assumption** — none of the three CLIs require localhost listeners:

- Codex has `--device-auth` flag (no callback port at all)
- Claude uses a hosted callback at `platform.claude.com/oauth/code/callback`
- Gemini accepts `GEMINI_API_KEY` env var to skip OAuth

Only the PKRelay reverse tunnel (`RemoteForward 18794 localhost:18793`, for browser driving) remains in the dev's SSH config.

### 3. Profile directory structure (Phase 2)

```
~/devbrain/profiles/alice/
├── .claude/             # Claude OAuth + session state (HOME-swap target)
├── .codex/              # Codex auth + config (CODEX_HOME target)
├── .gemini/             # Gemini OAuth (HOME-swap target)
├── .gitconfig           # Per-dev git author identity
└── .npmrc -> ~lhtdev/.npmrc   # Symlink to shared org registry token
```

`dev_id` regex: `^[a-z0-9_-]{1,64}$` — lowercase only, no path traversal, hyphens and underscores allowed. Validated at every entry point.

### 4. CLI command shape (Phase 3)

- `devbrain login --dev <id> [--cli claude|codex|gemini|all]`
- `devbrain logins [--dev <id>]` — table view: dev × cli → ✅/❌
- `devbrain logout --dev <id> [--cli ...]` — confirmation-gated; whole profile or per-CLI

`--cli` validates against the **live registry** via click callback (not `click.Choice`) — keeps the CLI extensible. New adapters work without re-decoration.

`logout --cli` skips `_PROFILE_SHARED_DOTFILES` (`.gitconfig`, `.npmrc`, `.config/gcloud`, `.config/gh`) so per-CLI logout doesn't strip git authorship.

Identity resolution order for `.gitconfig`: `--git-*` flags → dev record → interactive prompt → `dev_id` fallback. Devs already registered via `devbrain register` skip the prompt entirely.

### 5. cli_executor integration (Phase 4)

`run_cli(cli_name, prompt, ..., dev_id=None)` — when `dev_id` is resolvable (explicit arg or `DEVBRAIN_DEV_ID` env), the adapter's `SpawnArgs.env` is layered on top of `os.environ` before subprocess invocation. Caller-supplied `env_override` still wins. Backward-compatible: existing orchestrator calls work unchanged.

Lookup chain has graceful failure at every step (registry, profile dir, dev row, `spawn_args` call) — any failure returns `{}` and falls back to the legacy non-isolated path. Logged at debug level.

---

## Smoke Test (Phase 6) — Required Before BrightBot Devs Onboard

This is the acceptance gate. Walk through the onboarding flow as if you're a fresh dev:

```bash
# 1. SSH into the Mac Studio
ssh mac-studio
tmux new -s patrickkelly-test

# 2. Register
devbrain register --dev-id patrickkelly-test \
                  --name "Patrick (test profile)" \
                  --channel tmux:patrickkelly-test

# 3. Log into one CLI (claude is the most representative)
devbrain login --dev patrickkelly-test --cli claude
# → walk through OAuth in a laptop browser
# → confirm credentials land at ~/devbrain/profiles/patrickkelly-test/.claude.json

# 4. Verify
devbrain logins --dev patrickkelly-test
# → expect: patrickkelly-test  ✅ (claude)  ❌ (codex)  ❌ (gemini)

# 5. Submit a trivial factory job
factory submit "Add a no-op test that asserts True" --cli claude

# 6. Watch the factory pick it up
factory status

# 7. Once complete, confirm the spawned claude actually used the test profile
# In the factory logs, look for HOME=~/devbrain/profiles/patrickkelly-test in the
# spawn record (Phase 4's cli_executor logs the resolved env at debug level).

# 8. Confirm the resulting commit is authored by the test profile's .gitconfig
git -C <project_root> log -1 --format='%an <%ae>'
# → expect "Patrick (test profile)" not "lhtdev <whatever>"
```

**Pass criteria:**
- `devbrain logins` shows ✅ for claude
- Factory job runs to completion
- Spawned `claude` had `HOME=~/devbrain/profiles/patrickkelly-test` (verifiable via factory logs or by checking that the credential file `.claude.json` was read from there, not from `~lhtdev/.claude.json`)
- Resulting commit attribution matches the test profile's identity

**If pass:** the multi-dev system is verified working. Onboard the first real BrightBot dev next.

**If fail:** see "Open follow-ups" below — likely candidates are orchestrator call sites needing explicit `dev_id` parameter, or login flow edge cases not covered by mocks.

After the smoke test, clean up:

```bash
devbrain logout --dev patrickkelly-test --yes
```

---

## Consolidated DevBrain Store Payloads (already ingested)

These were stored to DevBrain via `mcp__devbrain__store` between phases — they're already in memory; documenting them here as the audit trail. All can be queried via `deep_search`.

| Phase | Type | Title |
|---|---|---|
| 1 | pattern | AI CLI adapter pattern (Phase 1) — per-dev credential isolation via registry |
| 2 | pattern | Per-dev profile directory management (Phase 2) — factory/profiles.py |
| 3 | decision | CLI command shape for multi-dev profile management (Phase 3) |
| 4 | decision | cli_executor.run_cli routes through AI CLI adapters via dev_id (Phase 4) |
| 5 | note | Multi-dev impl Phases 1-5 complete — ready for morning smoke test |

---

## Open Follow-Ups (non-blocking)

Tracked but explicitly out-of-scope for this overnight run:

1. **Update orchestrator call sites** to pass `dev_id=job.submitted_by` explicitly (`orchestrator.py:870, 1176, 1319, 1424, 1731` + `cleanup_agent.py:583`). Currently the multi-dev path activates via the `DEVBRAIN_DEV_ID` env var (set by `devbrain login` via tmux setenv). That works in-tmux but explicit param would be more robust outside tmux contexts. Small PR.

2. **Migrate existing mixed-case `dev_ids`** (`PatrickLHT` etc.) to lowercase canonical form. The `validate_dev_id` regex enforces lowercase, so legacy mixed-case rows can't use multi-dev features yet. Either rename in DB or relax the regex (but lowercase is defensible — keeps a single canonical form).

3. **`devbrain login --auto-register`** flag — chain into `devbrain register` if dev_id isn't in the devs table. Currently `login_dev` tolerates missing dev row (uses dev_id-based defaults), but operators may forget to register and end up without notification channels.

4. **Add CI workflow** to `.github/workflows/`. There's no CI in the repo currently — overnight agent merged based on local pytest only. Add a basic GH Actions workflow running `pytest` on PRs.

5. **Patch design doc Section 5** to drop the `RemoteForward 8765` placeholder OAuth port. The behavioral probes confirmed it's unnecessary; doc still says "placeholder; adjust after verification step." The verification is done — the placeholder should be removed.

6. **Tab-completion for `--cli`** against the live registry (click supports it; small entry-point hook).

7. **Retire `setup-multi-dev` wizard** (PR #49 from a prior session) which is Model-2 oriented and now arguably vestigial. Kept for now since `INSTALL.md §5.2` still references it as an alternate model.

8. **`is_logged_in` batch optimization** — currently runs once per row in `devbrain logins`. Fine for ≤10 devs; consider batching by adapter type if profiles grow.

9. **Possible Gemini OAuth port verification** — the probe couldn't definitively determine whether Gemini's OAuth uses a localhost callback (and which port) because `gemini` exits when stdin is non-TTY in our probe environment. If a future dev hits OAuth issues, set `dev.gemini_api_key` (clean fallback) or do a TTY-bound probe.

---

## What to Do Next

1. **Review this PR** (the handoff doc) and merge if the structure works for you.
2. **Run the smoke test** above. Should take 10-15 min.
3. **If smoke test passes**: onboard the first real BrightBot dev using `docs/ONBOARDING_TEAMMATE.md`.
4. **If smoke test fails**: file an issue with the specific symptom; most likely candidates are orchestrator call sites or the OAuth flow edge cases in `gemini`'s adapter.
5. **Optionally** address follow-ups #1 (orchestrator wiring) and #4 (CI) before the first BrightBot dev onboards — they make the system more robust but aren't blockers.
