# Multi-Dev Per-Dev Profile Routing — Implementation Plan

**Date:** 2026-04-28
**Design doc:** [2026-04-28-multi-dev-home-profiles-design.md](./2026-04-28-multi-dev-home-profiles-design.md)
**Audience:** an autonomous Claude Code agent + Patrick (morning review)
**Estimated runtime:** 4–6 hours autonomous

---

## Operating Principles for the Autonomous Agent

1. **Best-effort, validated at smoke test.** Where definitive verification requires interactive OAuth (which the agent can't do), the agent codes against documented best-effort assumptions, marks them clearly in adapter docstrings, and notes them in the PR body. The morning smoke test is the verification gate.

2. **Local pytest is the merge gate.** No CI exists in the repo. Before any push, the agent runs `python -m pytest -x --tb=short` and only proceeds if green. Branch protection is OFF on main, so `gh pr merge --auto --squash` will merge as soon as the PR has no failing required checks (there are none).

3. **One PR per phase.** Each phase is its own branch off latest main, its own PR, its own merge. Sequential — never parallel.

4. **Decisions stored in DevBrain.** After each phase merges, the agent calls `mcp__devbrain__store` with the decisions made + files changed + follow-ups noted. Use `type=decision` for architecture choices, `type=pattern` for reusable approaches.

5. **Halt conditions — store + stop:**
   - Local pytest fails after one retry → halt
   - PR conflicts with main → halt
   - GitHub API errors → halt
   - Cannot infer OAuth mechanism for an adapter from probes + docs → halt with PROBE_NOTES.md
   - Any phase takes more than 90 minutes → halt
   - Unexpected file outside the phase's scope is modified → halt
   - **On halt:** `mcp__devbrain__store(type=issue, ...)` with the blocker, then exit. Patrick reviews in the morning.

6. **Non-blocking issues → continue + note as follow-up.** Style nitpicks, code that could be refactored, missing edge cases that aren't load-bearing — note in `FOLLOWUPS.md` (create if missing) and keep moving.

7. **Commits use semantic prefixes.** `feat:`, `test:`, `docs:`, `chore:`, `refactor:` per the LHT versioning standard. Each PR's commits should fit a single semantic category.

8. **Co-authored-by in every commit:** `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

9. **Best practices research — order of consultation:**
   1. `mcp__devbrain__deep_search` first — has this been decided before?
   2. `mcp__docs-langchain__SearchDocsByLangChain` if LangChain-related (not for this project, mostly)
   3. WebFetch official vendor docs (Anthropic, OpenAI, Google) when adapter behavior is uncertain
   4. Read existing patterns in `factory/notifications/` — adapter layer mirrors that

---

## Pre-Flight (run before Phase 1)

```bash
cd ~/devbrain
git fetch origin && git checkout main && git pull --ff-only

# Verify tooling
source .venv/bin/activate
python -m pytest --collect-only 2>&1 | tail -3   # expect "382 tests collected" or more
gh auth status                                    # expect logged in as nooma-stack
which git tmux                                    # expect found

# Verify DevBrain MCP is reachable
# (If running inside Claude Code, mcp__devbrain__* tools should be loaded)
```

If any pre-flight check fails → halt + store issue.

### Behavioral probe (best-effort, partial)

For each CLI, observe what happens at login startup *without completing OAuth*:

```bash
# Claude — observe port binding behavior
mkdir -p /tmp/claude-probe && rm -rf /tmp/claude-probe/.claude
( HOME=/tmp/claude-probe timeout 10 claude /login 2>&1 & echo $! > /tmp/claude.pid ) &
sleep 3
lsof -p $(cat /tmp/claude.pid) -iTCP -sTCP:LISTEN 2>&1 | head -5  # ports it bound
kill $(cat /tmp/claude.pid) 2>/dev/null

# Codex — observe whether it's device-code (no port) or callback (port)
mkdir -p /tmp/codex-probe
( CODEX_HOME=/tmp/codex-probe timeout 10 codex login 2>&1 & echo $! > /tmp/codex.pid ) &
sleep 3
lsof -p $(cat /tmp/codex.pid) -iTCP -sTCP:LISTEN 2>&1 | head -5
kill $(cat /tmp/codex.pid) 2>/dev/null

# Gemini — same
mkdir -p /tmp/gemini-probe
( HOME=/tmp/gemini-probe timeout 10 gemini auth login 2>&1 & echo $! > /tmp/gemini.pid ) &
sleep 3
lsof -p $(cat /tmp/gemini.pid) -iTCP -sTCP:LISTEN 2>&1 | head -5
kill $(cat /tmp/gemini.pid) 2>/dev/null
```

Document findings (port number, or "device-code, no port") in `factory/ai_clis/PROBE_NOTES.md` (created in Phase 1 as part of the PR). If a CLI binds a port, that port goes into the adapter's `oauth_callback_ports`. If device-code, the adapter notes that and skips the tunnel pre-flight.

If the probe is inconclusive for any CLI → fall back to docs:
- Claude: WebFetch `https://code.claude.com/docs/en/auth` and similar
- Codex: WebFetch `https://github.com/openai/codex` README + docs
- Gemini: WebFetch `https://github.com/google-gemini/gemini-cli` and Google's CLI docs

If still inconclusive → assume callback on a port range like 8000–9000 (most common pattern for OAuth-via-localhost) and document the assumption clearly. Smoke test will validate.

---

## Phase 1 — AI CLI Adapter Module + 3 Adapters

**Branch:** `feat/multi-dev-ai-cli-adapters`
**Estimated:** 60–90 min

### Files

```
factory/ai_clis/__init__.py            # registry, default_registry export
factory/ai_clis/base.py                # AICliAdapter ABC, SpawnArgs, LoginResult, registry class
factory/ai_clis/auth_helpers.py        # verify_reverse_tunnel(port), check_listener(port)
factory/ai_clis/claude.py              # ClaudeAdapter
factory/ai_clis/codex.py               # CodexAdapter
factory/ai_clis/gemini.py              # GeminiAdapter
factory/ai_clis/PROBE_NOTES.md         # behavioral probe findings + assumptions per CLI
factory/ai_clis/test_base.py           # tests for ABC contract + registry
factory/ai_clis/test_claude.py         # tests for ClaudeAdapter
factory/ai_clis/test_codex.py          # tests for CodexAdapter
factory/ai_clis/test_gemini.py         # tests for GeminiAdapter
factory/ai_clis/test_auth_helpers.py
```

### TDD order (write tests first per file):

1. `test_base.py` — assert `AICliAdapter` is abstract, registry stores/returns by name, raises on duplicate registration, raises KeyError on unknown name
2. `base.py` — implement `AICliAdapter` ABC with `spawn_args`, `login`, `is_logged_in`, `required_dotfiles` abstracts. Implement `SpawnArgs` dataclass `{env: dict, argv_prefix: list[str]}`, `LoginResult` dataclass `{success: bool, error: str|None, hint: str|None}`. Implement `Registry` class.
3. `test_auth_helpers.py` — assert `verify_reverse_tunnel(port)` returns True if a listener exists on that port, False otherwise (use `socket.create_connection` against `127.0.0.1:<port>`)
4. `auth_helpers.py` — implement helpers
5. `test_codex.py` — assert `spawn_args(dev, profile_dir)` returns env with `CODEX_HOME=<profile>/.codex`, `GIT_CONFIG_GLOBAL=<profile>/.gitconfig`, `GIT_AUTHOR_NAME=<dev.full_name>`, `GIT_AUTHOR_EMAIL=<dev.email>`. `is_logged_in` returns True iff `<profile>/.codex/auth.json` exists. `login` invokes `codex login` subprocess with right env (mocked).
6. `codex.py` — implement
7. `test_claude.py` — assert `spawn_args(dev, profile_dir)` returns env with `HOME=<profile>` (string path), plus same git env vars. `is_logged_in` returns True iff `<profile>/.claude.json` exists.
8. `claude.py` — implement
9. `test_gemini.py` — same shape as claude (HOME-swap)
10. `gemini.py` — implement
11. `__init__.py` — populate `default_registry` with the 3 adapters, expose for import

### Tests must verify

- Path joining is correct (use `os.path.join` or `pathlib.Path`)
- env returned is a `dict[str, str]` (not Path objects)
- `validate_dev_id` is called or assumed to have been called by the time `spawn_args` runs (not adapter's job to validate, but tests should use valid IDs)
- `register` decorator/method on registry is idempotent

### Run before push

```bash
cd ~/devbrain && source .venv/bin/activate && python -m pytest factory/ai_clis/ -x --tb=short
```

All new tests pass. Existing 382 tests still pass (`python -m pytest -x --tb=short` whole repo).

### PR body template

```markdown
## Summary
Adds the AI CLI adapter layer per the design doc. Each adapter encapsulates
how its CLI is spawned with per-dev credentials. Mirrors the
`factory/notifications/` channel-pattern.

## Strategy per CLI
- Codex: explicit `CODEX_HOME` env var override (verified working)
- Claude: HOME-swap (no `CLAUDE_CONFIG_DIR` per official docs)
- Gemini: HOME-swap (no documented config-dir env var)
- Git authorship: `GIT_CONFIG_GLOBAL` + `GIT_AUTHOR_*` env vars on top

## Behavioral probe findings
See `factory/ai_clis/PROBE_NOTES.md` for OAuth callback mechanism per CLI.
Assumptions documented; smoke test validates.

## Test plan
- [x] `python -m pytest factory/ai_clis/ -x` (new tests pass)
- [x] `python -m pytest -x` (existing tests still pass)
- [ ] Smoke test with a fresh dev profile (gated to morning manual review)

## Out of scope
- Profile dir management (Phase 2)
- CLI commands (Phase 3)
- cli_executor wiring (Phase 4)

## Design doc
docs/plans/2026-04-28-multi-dev-home-profiles-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

### Merge

```bash
gh pr merge --auto --squash --delete-branch
```

### After merge — store in DevBrain

```python
mcp__devbrain__store(
  type="pattern",
  project="devbrain",
  title="AI CLI adapter pattern (Phase 1 — multi-dev profile routing)",
  content="<summary of the pattern, key files, decisions>",
  tags=["multi-dev", "factory", "adapter", "phase-1"],
)
```

---

## Phase 2 — Profile Management Module

**Branch:** `feat/multi-dev-profiles`
**Depends on:** Phase 1 merged

### Files

```
factory/profiles.py
factory/test_profiles.py
```

### TDD order

1. `test_profiles.py` — tests in this order:
   - `test_validate_dev_id_accepts_valid()` — `alice`, `bob_2`, `pkrelay-dev`
   - `test_validate_dev_id_rejects_path_traversal()` — `../etc/passwd`, `alice/../bob`, `/abs/path` — all raise
   - `test_validate_dev_id_rejects_empty_or_too_long()` — `""`, 65-char string raise
   - `test_validate_dev_id_rejects_special_chars()` — `alice!`, `alice@host`, `alice space` raise
   - `test_get_profile_dir_creates_if_missing()` — first call creates dir, subsequent calls return existing
   - `test_get_profile_dir_validates_id_first()` — invalid id raises before any fs work
   - `test_list_profiles_empty()` — returns `[]` when no profiles dir
   - `test_list_profiles_returns_dev_ids()` — creates fixtures, verifies list
   - `test_populate_gitconfig_writes_user_block()` — verifies `.gitconfig` contents include `[user]\n  name = ...\n  email = ...`
   - `test_refresh_shared_symlinks_creates_symlinks()` — uses tmp dir as fake `lhtdev`'s home, creates `.npmrc`, verifies symlink points correctly
   - `test_delete_profile_removes_dir()` — round-trip
2. `profiles.py` — implement against tests
3. Module exports: `get_profile_dir`, `validate_dev_id`, `list_profiles`, `delete_profile`, `populate_gitconfig`, `refresh_shared_symlinks`, `ProfileInfo`

### Behavioral notes

- Profile dir base: `<DEVBRAIN_HOME>/profiles/` (from config, defaults to `~/devbrain/profiles/`)
- `validate_dev_id` regex: `r"^[a-z0-9_-]{1,64}$"`
- `populate_gitconfig` is idempotent — overwrites existing
- `refresh_shared_symlinks` reads `factory.shared_dotfiles` from yaml config; defaults to `[".npmrc", ".config/gcloud", ".config/gh"]` if unset

### Merge + store

```python
mcp__devbrain__store(
  type="pattern",
  project="devbrain",
  title="Per-dev profile directory layout (Phase 2)",
  content="<layout, validation regex, symlink config>",
  tags=["multi-dev", "factory", "profiles", "phase-2"],
)
```

---

## Phase 3 — `devbrain login` / `logins` / `logout` CLI Commands

**Branch:** `feat/multi-dev-cli-commands`
**Depends on:** Phase 2 merged

### Files

```
factory/dev_login.py        # business logic for login/logins/logout
factory/test_dev_login.py
cli.py                      # add @cli.command() handlers that call factory.dev_login
test_cli_login.py           # CLI-level click tests
```

### TDD order

1. `test_dev_login.py` — test the business logic in isolation:
   - `test_login_creates_profile_and_calls_adapter()`
   - `test_login_prompts_for_git_identity_first_run()`
   - `test_login_skips_git_identity_prompt_if_exists()`
   - `test_logins_returns_table_with_all_dev_x_cli_combinations()`
   - `test_logout_removes_specific_cli_creds()`
   - `test_logout_all_removes_whole_profile()`
2. `dev_login.py` — implement business logic. Functions take `Registry` + `profiles` + an io adapter (for prompts, capture in tests).
3. `test_cli_login.py` — click `CliRunner` tests for each command's flag handling
4. `cli.py` — wire up commands, defer to `factory.dev_login` for actual work

### Command shapes

```bash
devbrain login --dev <id> [--cli claude|codex|gemini|all]
# First-run prompts: git name, git email
# Then runs adapter.login() per chosen CLI(s)
# Sets DEVBRAIN_DEV_ID via tmux setenv if running inside tmux

devbrain logins [--dev <id>]
# Renders rich.Table or simple text:
#   dev_id    | claude | codex | gemini
#   alice     | ✅     | ✅    | ❌
#   bob       | ✅     | ❌    | ❌

devbrain logout --dev <id> [--cli ...]
# Without --cli: prompts for confirmation, removes whole profile dir
# With --cli: removes that CLI's subdir only
```

### tmux env capture

If `$TMUX` is set, run:
```python
subprocess.run(["tmux", "setenv", "DEVBRAIN_DEV_ID", dev_id])
```
After login completes. Document in command help that this only works inside tmux.

### Merge + store

```python
mcp__devbrain__store(
  type="decision",
  project="devbrain",
  title="CLI command shape for multi-dev profile management (Phase 3)",
  content="<decisions: command names, flags, prompts, tmux setenv>",
  tags=["multi-dev", "cli", "phase-3"],
)
```

---

## Phase 4 — `cli_executor.run_cli` Integration

**Branch:** `feat/multi-dev-cli-executor`
**Depends on:** Phases 1, 2, 3 merged

### Files

```
factory/cli_executor.py     # modify run_cli signature + body
factory/test_cli_executor.py # add new tests; do NOT delete existing tests
```

### Modifications

`run_cli` current signature: `run_cli(prompt, *, ..., env_override=None)`. Add `cli_name: str` and `dev_id: str` parameters (with sensible defaults for backward compat — default cli_name from config, default dev_id from `os.environ.get("DEVBRAIN_DEV_ID", "lhtdev")`).

Inside `run_cli`:
```python
from factory.ai_clis import default_registry
from factory import profiles
from factory.devs import get_dev  # whatever the existing accessor is

adapter = default_registry.get(cli_name)
profile_dir = profiles.get_profile_dir(dev_id)
dev = get_dev(dev_id)  # raises if not registered
spawn = adapter.spawn_args(dev=dev, profile_dir=profile_dir)

env = {**os.environ, **spawn.env, **(env_override or {})}
argv = [*spawn.argv_prefix, *additional_args]
return subprocess.run(argv, env=env, ...)
```

### Tests

- Mock the registry + profiles, assert `run_cli("test", cli_name="claude", dev_id="alice")` builds env with `HOME=/path/to/alice/profile`
- Same for codex with `CODEX_HOME=...`
- Verify caller's `env_override` wins over adapter's env (caller can still override individual vars)
- Existing tests must still pass (backward-compat default behavior)

### Halt condition

If existing tests start failing because of the signature change → halt (the change broke a real caller). Don't blindly fix by mass-editing call sites.

### Merge + store

```python
mcp__devbrain__store(
  type="decision",
  project="devbrain",
  title="cli_executor.run_cli routes spawn through adapters (Phase 4)",
  content="<the wiring decision, signature change, backward-compat defaults>",
  tags=["multi-dev", "factory", "cli-executor", "phase-4"],
)
```

---

## Phase 5 — Onboarding Doc Rewrite (Model 1)

**Branch:** `docs/multi-dev-model-1-onboarding`
**Depends on:** Phases 1–4 merged (so the doc references real shipped commands)

### Files

```
docs/ONBOARDING_TEAMMATE.md   # NEW (replaces deleted Model-2 version)
INSTALL.md                     # add cross-link in §5.2 to the new doc
```

### Content sections

1. **Architecture diagram** — Alice's laptop ↔ Mac Studio with reverse tunnels labeled (PKRelay 18793, OAuth callback ports per CLI from PROBE_NOTES.md)
2. **Prereqs** — Mac, SSH key on Alice's laptop, PKRelay extension
3. **Step 1: Patrick adds Alice's pubkey** to `lhtdev@mac-studio:~/.ssh/authorized_keys`
4. **Step 2: SSH config** — full block with all `RemoteForward` lines
5. **Step 3: SSH in, create persistent tmux session**
6. **Step 4: `devbrain register --dev alice --name "Alice ..." --channel tmux:alice`**
7. **Step 5: `devbrain login --dev alice --cli claude` then `--cli codex --cli gemini`** — covers git identity prompt, OAuth flow, tmux setenv
8. **Step 6: Verify** — `devbrain logins --dev alice`, submit hello-world factory job, watch for tmux popup
9. **Day-2 ops** — re-login on token expiry, leaving the team, switching tmux sessions
10. **Troubleshooting** — OAuth callback hang (check RemoteForward), missing profile, factory stuck in `needs_human`

### Merge + store

```python
mcp__devbrain__store(
  type="note",
  project="devbrain",
  title="Model 1 onboarding doc replaces Model 2 (Phase 5)",
  content="<doc location, replaces docs/ONBOARDING_TEAMMATE.md (deleted in earlier session)>",
  tags=["multi-dev", "docs", "onboarding", "phase-5"],
)
```

---

## After Phase 5 — Halt for Phase 6 Smoke Test (Patrick's morning)

The agent is **done** after Phase 5 merges. Final actions before exit:

1. `mcp__devbrain__store(type="session", title="Multi-dev impl Phases 1–5 complete", content="<full summary>")` 
2. Update WORK_LOG.md with handoff notes (link to all 5 PRs, halt point, what Phase 6 needs)
3. Exit cleanly. Patrick's morning workflow:
   - Review the 5 merged PRs
   - Run smoke test (Phase 6) — `devbrain login --dev patrickkelly-test --cli claude` interactively
   - Submit a hello-world factory job, verify it spawns under the test profile's credentials
   - Confirm git commit attribution matches the test profile's identity

If Phase 6 reveals adapter bugs (assumed wrong port, etc.) → fast-follow PR is needed; agent does NOT attempt this. Patrick's call.

---

## Halt Conditions Cheat Sheet (one-liner per row)

| Condition | Action |
|---|---|
| pytest fails after retry | store(issue), exit |
| PR merge conflict | store(issue), exit |
| GitHub API timeout/error | store(issue), exit |
| Adapter probe inconclusive AND docs unclear | store(issue) with PROBE_NOTES.md state, exit |
| Phase >90 min | store(issue), exit |
| Modifying file outside scope | store(issue), exit |
| Required check (CI) fails on PR | store(issue), exit |

---

## Pre-Flight Checklist for the Agent (run once at start)

- [ ] `git fetch && git checkout main && git pull --ff-only` succeeds
- [ ] `python -m pytest --collect-only` collects ≥382 tests
- [ ] `gh auth status` confirms login
- [ ] `mcp__devbrain__get_project_context(project="devbrain")` returns OK
- [ ] Behavioral probes for claude/codex/gemini run without errors (findings stored in working notes)
- [ ] No uncommitted changes in working tree

If any unchecked → halt + store + exit.

---

## Decision Log Convention

Each phase's `mcp__devbrain__store(type="decision", ...)` should include in the `content` field:

- **What** the decision was (prose)
- **Why** the alternative was rejected (one or two sentences)
- **Where** in the code it lives (file paths)
- **Tradeoffs accepted**

This keeps the design+impl decision history queryable for future sessions.
