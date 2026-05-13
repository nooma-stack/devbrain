# Multi-CLI Invitation Kit — Design

> **Status:** Design proposal — open questions in §10. Not yet locked.
>
> **Scope:** Let one invitation onboard a dev for multiple (CLI, agent_app)
> targets in a single kit. Today `invitations.cli` is a single value; this
> design generalizes it to a `cli_targets` array, allows the dev to pick
> any subset at runtime, and tracks per-target activation state inside the
> same invitation row.
>
> **Non-goals:** Adding new CLI families beyond the current
> `(claude, codex, gemini)` set. Building a UI for invitation management.
> Solving codex/gemini auth flows (assumed to exist or be added in
> separate work; this design doesn't block on them).
>
> **Verification gate:** New + existing postulates green; existing
> single-CLI invitations migrated cleanly; round-trip test (create
> invitation → render kit → activate 2 of 3 targets → 3rd target added
> later in a follow-up flow without disturbing the activated 2).

---

## 1. Drivers

**Primary:** lower the per-dev onboarding cost. Today, each dev who uses
multiple Anthropic surfaces (Claude Code CLI on a Mac, Claude Desktop on a
Windows machine, etc.) needs N separate invitations from the admin — N
emails, N kit files, N pubkey rotations, N OAuth flows. Mike's onboarding
this week burned half a day in part because his Claude Desktop setup
required a fresh invitation after he'd already gone through the Claude
Code flow on a prior attempt.

**Secondary:** make multi-machine work pleasant. A dev who works on a
laptop AND a desktop wants both registered with the same DevBrain dev
identity. Today that requires careful coordination of which invitation
gets sent to which device — error-prone, no shared state.

**Pain points addressed:**

- The "I'm onboarded for Claude Code but I want to add Claude Desktop"
  case today requires opening a new invitation, which means a new
  bootstrap SSH key, which means the admin has to be available.
- `invitations.cli` being scalar forces a UNIQUE-ish constraint by
  convention (one active invitation per dev at a time). Plus the kit
  filename collides if you send two: `mike_courtney-onboard.md` gets
  overwritten by the second one.
- The `agent_app` value (`claude-code` vs `claude-desktop`) is currently
  captured in the kit Markdown but NOT stored in the DB — so a SQL query
  can't tell which app a dev is using.

---

## 2. Locked decisions (proposed)

| # | Decision |
|---|---|
| 1 | One invitation per dev maps to one or more `(cli, agent_app)` targets. |
| 2 | Schema: replace `invitations.cli text` with `invitations.cli_targets jsonb`. Single value becomes a one-element array. |
| 3 | Each target is `{cli, agent_app, status, pubkey_received_at, oauth_token_received_at, oauth_token}`. |
| 4 | Target `status` ∈ `pending` \| `active` \| `failed` \| `revoked`. |
| 5 | Invitation `status` rolls up: `activated` when **any** target is `active`; `pending` when all are `pending`. |
| 6 | Kit ships with rotation flows for every selected target. Dev's agent runs each flow in sequence at activation time. |
| 7 | Per-target on-disk auth lives under `profiles/<dev>/<cli>/` (matches current Claude convention; extends to codex/gemini). |
| 8 | Bootstrap SSH key remains one-per-invitation. Same key authorizes all per-target rotation handlers in the kit. |
| 9 | Re-onboarding to add a new target: admin issues a SECOND invitation with `cli_targets=[only the new one]`; rotation handler appends to the existing invitation row's `cli_targets` array. |
| 10 | Migration: backfill existing rows with `cli_targets=[{cli: <existing.cli>, agent_app: 'unknown', status: <derived>, ...}]`. Keep `invitations.cli` as a generated column for one release. |

---

## 3. Architecture

### Schema delta

```sql
ALTER TABLE devbrain.invitations
    ADD COLUMN cli_targets jsonb NOT NULL DEFAULT '[]';

-- Backfill from existing scalar cli column.
UPDATE devbrain.invitations SET cli_targets = jsonb_build_array(
    jsonb_build_object(
        'cli', cli,
        'agent_app', 'unknown',
        'status', CASE
            WHEN status = 'activated' THEN 'active'
            WHEN status = 'revoked'   THEN 'revoked'
            ELSE 'pending'
        END,
        'pubkey_received_at', pubkey_received_at,
        'oauth_token_received_at', oauth_token_received_at,
        'oauth_token', oauth_token
    )
);

-- Generated column for one-release backward compat.
ALTER TABLE devbrain.invitations
    DROP COLUMN cli,
    ADD COLUMN cli text GENERATED ALWAYS AS (
        cli_targets->0->>'cli'
    ) STORED;

-- New index for "show me all invitations with claude-code target"
CREATE INDEX invitations_cli_targets_gin
    ON devbrain.invitations USING gin (cli_targets);
```

### Target object shape

```jsonc
{
  "cli": "claude" | "codex" | "gemini",
  "agent_app": "claude-code" | "claude-desktop" | "codex-cli" | "gemini-cli",
  "status": "pending" | "active" | "failed" | "revoked",
  "pubkey_received_at": "2026-05-13T20:23:05Z" | null,
  "oauth_token_received_at": "2026-05-13T20:25:11Z" | null,
  "oauth_token": "sk-ant-oat01-...",  // null until oauth flow runs
  "failed_reason": "..."               // populated only on status=failed
}
```

`agent_app` makes the (claude-code vs claude-desktop) distinction explicit
and queryable; we lose it today. The two clients have different OAuth
flows (one uses `claude setup-token` directly, the other uses Desktop's
internal browser handoff) so persisting which one a dev is on matters
for future support / debugging.

### Top-level invitation status rollup

The existing `invitations.status` column stays but its values change:

| Value     | Meaning |
|-----------|---------|
| `pending` | No target has activated yet. |
| `ready`   | All requested targets received pubkeys; awaiting reconciler. |
| `partial` | NEW — at least one target is active, at least one is still pending or failed. |
| `activated` | All requested targets are active. |
| `expired` | Invitation TTL elapsed; remaining-pending targets timed out. |
| `revoked` | Admin revoked the invitation; all targets force-revoked. |

`partial` is the new value that makes multi-target practical — a dev who
got Claude Code working but hit a Codex auth bug isn't stuck; their
Claude Code is `active` and they can finish Codex later.

---

## 4. Kit rendering

The kit (`onboarding/<dev>-onboard.md`) currently has ONE rotation flow
hardcoded for the dev's `cli`. The new generator emits N flow blocks,
one per requested `(cli, agent_app)` target.

Kit structure becomes:

```markdown
# Welcome, Mike! — DevBrain onboarding kit

You've been invited to onboard for these targets:
  - claude on claude-desktop
  - codex on codex-cli

Please run the flow for EACH target. The flows are independent — if one
fails, the others can still complete.

## Target 1 — claude on claude-desktop
<flow content as today, parameterized for this target>

## Target 2 — codex on codex-cli
<flow content for codex>

## Already-activated targets
<list, if re-onboarding>

## Troubleshooting
<unchanged>
```

The rendering function takes the `cli_targets` array and dispatches each
to a per-`(cli, agent_app)` template snippet. Existing
`onboarding_kit.py` helpers stay; we add a registry of per-target
templates and iterate the array.

---

## 5. Onboarding flow (runtime)

```
1. Admin runs: devbrain setup add-dev (interactive: picks cli_targets)
                or: devbrain invite --dev=mike --target=claude:desktop --target=codex:cli
2. invitations row created with cli_targets=[{cli:claude, agent_app:claude-desktop, status:pending},
                                             {cli:codex,  agent_app:codex-cli,      status:pending}]
3. Bootstrap SSH key written to authorized_keys; kit file generated;
   email sent.
4. Dev opens kit, drops it in their agent. Agent SSHes in with bootstrap
   key and runs the first flow:
   - rotate pubkey for target 1 → reconciler activates
   - run setup-token flow for target 1 → on-disk file written
   - update cli_targets[0].status = 'active'
   - call invitation_status_rollup() to recompute top-level status
5. Agent moves to target 2, repeats.
6. When all targets reach 'active', top-level status = 'activated'.
7. If a target fails (e.g. codex setup-token unsupported): mark target
   status='failed' with failed_reason, leave invitation status='partial'.
   Admin can issue a follow-up invitation later.
```

Existing `onboard_rotate.sh` + `onboard_rotate_helper.py` are
generalized to accept a `target_index` argument and update the matching
array element rather than the scalar columns.

---

## 6. Per-CLI on-disk paths

| CLI | Path under `profiles/<dev>/` | Format |
|---|---|---|
| claude | `.claude/oauth-token` | bare text, single line, `sk-ant-oat01-...` |
| codex  | `.codex/auth.json` (TODO confirm) | JSON `{access_token, refresh_token, ...}` |
| gemini | `.gemini/api-key` (TODO confirm) | bare text, `AIza...` |

The kit's per-flow snippet writes to the correct path for its `cli`.
DevBrain's adapter layer (`factory/ai_clis/claude.py`, etc.) reads from
the same path at runtime.

---

## 7. Migration plan

**Forward migration (one PR):**

1. Schema migration `037_invitations_cli_targets.sql`:
   - Add `cli_targets jsonb NOT NULL DEFAULT '[]'`
   - Backfill from `cli` column (see §3 SQL)
   - Drop `cli` column; replace with generated column from `cli_targets[0].cli`
   - Add GIN index
2. Code migration: update `invitations.py`, `onboard_rotate_helper.py`,
   `onboarding_kit.py` to use `cli_targets` array. Keep
   `Invitation.cli` Python property as a backward-compat alias that
   reads `cli_targets[0].cli`.
3. Update `cognify_extract` + adapter resolvers — none of them currently
   read `invitations.cli`, but should be audited.
4. Run end-to-end test against Mike's row + 5 fixture rows. Verify
   `cli`-as-generated-column matches pre-migration value.

**Rollback:** generated column comes off, scalar column comes back from
the `cli_targets[0].cli` snapshot. One-way migrations are unsafe for
this; the design keeps the generated column as the rollback bridge.

**Existing-invitations behavior:** unchanged. A pre-migration single-CLI
invitation becomes a one-element `cli_targets` array; all downstream
queries that read `cli` still work via the generated column.

---

## 8. CLI + MCP changes

**`devbrain setup add-dev`** grows a multi-pick prompt:
```
Which Anthropic surfaces will Mike use? (space-separated, comma-separated, or 'all')
  1. claude-code (Claude Code CLI)
  2. claude-desktop (Claude Desktop app)
  3. codex-cli
  4. gemini-cli
Selection [1]: 1 2
```
Selection defaults to the dev's primary CLI from a config preference,
matches today's single-CLI behavior when picking only one.

**`devbrain invite`** (new non-interactive flavor for scripting):
```
devbrain invite --dev mike_courtney \
    --target claude:claude-desktop \
    --target codex:codex-cli \
    --email mike@lighthouse-therapy.com
```

**`devbrain invitations list`** shows per-target status:
```
mike_courtney   partial    claude-desktop:active  codex-cli:pending
patrick         activated  claude-code:active
```

**`devbrain send-invite`** (existing) renders the multi-target kit.

**MCP**: no surface changes. The MCP `factory_*` tools that touch
invitations all read via the Python `Invitation` class, which gets the
backward-compat property.

---

## 9. Postulates / tests

| # | Postulate |
|---|---|
| 1 | Creating an invitation with `cli_targets=[A, B]` stores both targets with status=pending. |
| 2 | After a pubkey rotation for target index 0, only `cli_targets[0]` is updated. |
| 3 | After OAuth submission for target index 1, only `cli_targets[1]` gets `oauth_token_received_at`. |
| 4 | Top-level `status` rolls up correctly: `activated` iff all targets active, `partial` if mixed, `pending` if none active. |
| 5 | Kit renderer emits N flow sections for N targets. |
| 6 | Kit renderer skips already-active targets when invitation is in `partial` state and dev re-runs the kit. |
| 7 | `Invitation.cli` Python property returns `cli_targets[0].cli` (back-compat). |
| 8 | Schema generated column `cli` matches `cli_targets[0]->>'cli'` for all rows. |
| 9 | Existing single-CLI invitation rows survive migration with `cli_targets.length=1` and unchanged `cli` value. |
| 10 | Per-target failure (e.g. codex setup fails) leaves other targets unaffected. |

---

## 10. Open questions

1. **Codex + Gemini auth flow status.** The `cli IN ('claude','codex','gemini')`
   CHECK constraint accepts all three, but my read of the adapter code
   only shows `claude` is implemented end-to-end. Are codex/gemini just
   placeholders today? If yes, Phase 9 (this design) should explicitly
   declare them as future-work targets and ship multi-target for claude
   only initially.
2. **Kit re-run UX when partial.** If Mike has claude-desktop active and
   wants to add codex later: does the admin issue a new invitation with
   only codex, or modify the existing invitation? Decision affects
   §5's "re-onboarding" path.
3. **Bootstrap SSH key reuse.** Today the bootstrap key has a tight TTL
   (3 days) and self-deletes after one rotation. For multi-target, the
   first rotation deletes the key — making targets 2..N stuck without
   ssh access. Design needs: either (a) keep key alive until ALL
   targets activate, (b) issue N bootstrap keys, (c) target 1 also
   appends the dev's permanent key so 2..N use the permanent key.
4. **agent_app values.** Are `claude-code` and `claude-desktop` the
   only Claude variants? What about future iOS/Android Claude apps?
   Should the field be free-text or enum?
5. **Per-target expiration.** A dev who activates 2 of 3 targets and
   ignores the 3rd for a week: does the 3rd auto-expire after the
   invitation TTL even though the invitation is `partial`? My instinct
   is yes (target expires, invitation moves to `activated` once the
   remaining-pending count hits zero). Confirm with Patrick.
6. **Migration safety on Mike.** Mike's current row needs to survive
   the migration with `cli_targets=[{cli:'claude', agent_app:'claude-desktop', status:'active', ...}]`. The agent_app value isn't stored
   anywhere today — should we backfill it from the kit filename pattern
   (`-onboard.md`), the email subject, or set to `'unknown'` and let
   subsequent activations correct it?

---

## 11. Implementation phasing

- **9a:** Schema migration + Python `Invitation` class refactor +
  backfill (no kit changes yet). End: existing single-CLI flow still
  works, `cli_targets` is populated for all rows.
- **9b:** Kit renderer multi-target support. Tests against fixture
  invitations with 1, 2, 3 targets.
- **9c:** `onboard_rotate_helper.py` per-target updates. Postulates 1-4.
- **9d:** CLI changes (`add-dev`, `invite`, `invitations list`).
- **9e:** Bootstrap key lifecycle fix (open question #3) — design
  decision needed; implementation depends on it.
- **9f:** End-to-end test: issue 2-target invitation for `test_dev_multi`,
  run kit through bootstrap → 2 activations → verify state.

Phases 9a-9d can ship as one PR or split; 9e is a separate decision and
its own PR; 9f is a verification gate before declaring multi-target
complete.

---

## 12. Roll-back plan

Schema rollback was discussed in §7 (generated column survives, but
restoring scalar `cli` from `cli_targets[0]` is a one-step migration).
If we ship and discover the multi-target flow has a sharp edge:

- Code-side: `Invitation.cli` property keeps working (reads `cli_targets[0]`).
- New invitations: admin can issue single-target only (`--target claude:claude-code` once). Existing multi-target rows stay readable.
- Kit renderer: degrades to single-target if `cli_targets.length == 1` (this is the existing behavior shape).

Worst case: stop issuing multi-target invitations, single-target flow
continues unchanged. No data loss.
