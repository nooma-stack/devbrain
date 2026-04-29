# Multi-Dev Per-Dev Profile Routing — Design

**Date:** 2026-04-28
**Status:** Design validated, ready for implementation
**Owner:** Patrick Kelly (PatrickLHT)
**Architectural decision basis:** [DevBrain decision 2026-04-16, "Multi-dev Claude subscriptions via HOME-directory profiles"]

---

## Problem

DevBrain's factory currently spawns AI CLIs (`claude`, `codex`, `gemini`) under whatever credentials happen to be on the macOS user owning the install (`lhtdev`). When multiple devs SSH into the shared LHT Mac Studio and submit factory jobs, every job runs against the same Claude Max subscription, the same Codex login, the same Gemini auth. This is wrong on three axes:

1. **Per-account TOS** — Anthropic's, OpenAI's, and Google's subscription terms are per-individual. Sharing one auth across multiple humans is a violation.
2. **Wrong attribution** — git commits made by AI CLIs inside a factory job get authored by `lhtdev`, not by the dev who submitted the job. The `submitted_by` column on `factory_jobs` knows who submitted, but the actual subprocess inherits the host user's identity.
3. **Subscription bills/quotas** — Alice's expensive Claude Max session steals tokens from Bob's, with no visibility into who consumed what.

The architectural decision (locked 2026-04-16) was per-dev HOME-directory profiles. **Implementation is not started.** This document is the design for that implementation.

## Operating Model (recap, locked)

- Mac Studio (LHT MS) hosts the shared DevBrain instance; the factory orchestrator runs there
- Devs SSH into `lhtdev@mac-studio` from their laptops, attach a tmux session named after their `dev_id`, run interactive Claude/Codex/Gemini sessions there
- Devs submit factory jobs from their tmux session; the orchestrator picks them up and spawns AI CLIs to execute phases
- For browser-driven UI work, the orchestration agent reaches the dev's local browser via SSH **reverse** tunnel to the laptop's PKRelay broker (already shipped)
- Factory tier-2 permissions apply (git_push OFF; pushes happen outside the AI subprocess at job-approval time)

## Trust Boundary

Same OS user, same DevBrain DB, same code checkout — devs trust each other (per the "trust the team" HIPAA decision). The per-dev profile is **credential isolation, not security isolation.** A motivated dev with shell access on the Mac Studio can read `~/devbrain/profiles/alice/`; that's accepted under the trust model.

## Architecture: AI CLI Adapter Layer

`factory/ai_clis/` mirrors the existing `factory/notifications/` pattern: an abstract `AICliAdapter` base class plus per-CLI subclasses, registered in a default registry. The factory orchestrator and the new `devbrain login` / `devbrain logins` commands all go through `registry.get(cli_name).{spawn_args, login, is_logged_in}()`.

Each adapter encapsulates the per-CLI mechanism for credential isolation:

| CLI | Mechanism | Reason |
|---|---|---|
| `codex` | `CODEX_HOME=<profile>/.codex` | Native env var supported (verified) |
| `claude` | `HOME=<profile>` (HOME-swap) | No `CLAUDE_CONFIG_DIR` exists per official docs |
| `gemini` | `HOME=<profile>` (HOME-swap) | No documented config-dir env var |

The HOME-swap is constrained to the single AI subprocess invocation. The factory orchestrator's `HOME` and broader environment stay untouched. Git author identity is set explicitly via `GIT_CONFIG_GLOBAL`, `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL` env vars on top of whichever isolation mechanism the adapter uses, ensuring per-dev attribution regardless.

### Boundary: orchestration vs. protocol

The adapter owns OAuth **orchestration** (calling the CLI's native login flow with the right env, pre-flight checking reverse tunnels, picking auth strategy, verifying success, surfacing recovery messages on failure). The adapter does **not** reimplement OAuth itself — no hand-rolled OAuth clients, no custom callback servers, no token refresh logic. We point each CLI at the right directory and let it manage its own auth state. When Anthropic/OpenAI/Google ship auth-flow improvements, we inherit them by virtue of calling the native login flow.

## Components

### 1. `factory/ai_clis/`

```
factory/ai_clis/
├── __init__.py          # default_registry, register()
├── base.py              # AICliAdapter ABC, SpawnArgs dataclass, registry
├── claude.py            # ClaudeAdapter (HOME-swap)
├── codex.py             # CodexAdapter (CODEX_HOME, precise)
├── gemini.py            # GeminiAdapter (HOME-swap)
├── auth_helpers.py      # verify_reverse_tunnel(port), oauth port discovery
└── tests/
    ├── test_base.py
    ├── test_claude.py
    ├── test_codex.py
    └── test_gemini.py
```

Base interface:

```python
class AICliAdapter(ABC):
    name: ClassVar[str]
    oauth_callback_ports: ClassVar[list[int]]  # for tunnel pre-flight

    @abstractmethod
    def spawn_args(self, dev: Dev, profile_dir: Path) -> SpawnArgs:
        """Env overrides + argv prefix for invoking this CLI for this dev.
        Picks auth strategy: OAuth profile vs. API key, based on dev config."""

    @abstractmethod
    def login(self, dev: Dev, profile_dir: Path) -> LoginResult:
        """Pre-flight checks (tunnel up?), invoke CLI's native login,
        verify creds landed. Returns success/failure + actionable error."""

    @abstractmethod
    def is_logged_in(self, dev: Dev, profile_dir: Path) -> bool: ...

    @abstractmethod
    def required_dotfiles(self) -> list[str]: ...
```

### 2. `factory/profiles.py`

Per-dev profile directory management.

```python
def get_profile_dir(dev_id: str) -> Path: ...        # ensures dir exists, validates id
def validate_dev_id(dev_id: str) -> None: ...        # regex [a-z0-9_-]{1,64}
def list_profiles() -> list[ProfileInfo]: ...        # for `devbrain logins`
def delete_profile(dev_id: str) -> None: ...         # for `devbrain logout`
def populate_gitconfig(profile_dir, name, email): ...
def refresh_shared_symlinks(profile_dir): ...        # .npmrc, etc. from config
```

Profile dir layout:
```
~/devbrain/profiles/alice/
├── .claude/             # Claude OAuth + session state
├── .codex/              # Codex auth + config.toml
├── .gemini/             # Gemini OAuth + config
├── .gitconfig           # per-dev git identity
└── .npmrc -> ~lhtdev/.npmrc   # shared (symlink, configurable)
```

### 3. CLI commands (`cli.py`)

- `devbrain login --dev <id> [--cli claude|codex|gemini|all]`
- `devbrain logins [--dev <id>]`
- `devbrain logout --dev <id> [--cli ...]`

`login` flow:
1. `profiles.get_profile_dir(dev_id)` (mkdir if needed; validate dev_id)
2. On first run for this dev: prompt for git identity (name + email) → write `.gitconfig`
3. Pick adapter(s) from CLI flag; for each: `adapter.login(dev, profile_dir)`
4. Verify with `adapter.is_logged_in(...)`; print result

`logins` flow:
1. For each dev in `devs` table (or just `--dev`)
2. For each adapter in registry
3. Call `adapter.is_logged_in(...)`, render as table

### 4. `factory/cli_executor.py` modifications

```python
def run_cli(cli_name: str, dev_id: str, ..., env_override: dict = None):
    adapter = ai_clis.registry.get(cli_name)
    profile_dir = profiles.get_profile_dir(dev_id)
    spawn = adapter.spawn_args(dev=devs.get(dev_id), profile_dir=profile_dir)

    env = {**os.environ, **spawn.env, **(env_override or {})}
    argv = [*spawn.argv_prefix, *additional_args]

    return subprocess.run(argv, env=env, ...)
```

### 5. `factory/setup.py` extension

`setup_factory_permissions` wizard gains an optional final step: "set up your AI CLI logins now? [y/N]" — chains into `devbrain login` for the current `$USER`. Skippable.

### 6. Configuration (`config/devbrain.yaml`)

```yaml
factory:
  ai_clis:
    default: claude
    claude: { enabled: true }
    codex:  { enabled: true }
    gemini: { enabled: true }
  shared_dotfiles:
    - .npmrc
    - .config/gcloud
    - .config/gh
```

## Data Flow

### Job execution

```
1. Alice (in tmux on LHT MS) submits a factory job
   → CLI captures DEV_ID from $DEVBRAIN_DEV_ID (set in tmux env on login)
   → INSERT factory_jobs (submitted_by="alice", ...)

2. Orchestrator picks up queued job
   → dev = devs.get(job.submitted_by)        # "alice"
   → profile_dir = profiles.get_profile_dir(dev.id)

3. For each phase (planning/implementing/reviewing/qa)
   → cli_name = config.factory.ai_clis.<phase>.cli  (default: claude)
   → adapter = registry.get(cli_name)
   → spawn = adapter.spawn_args(dev, profile_dir)

4. cli_executor.run_cli applies spawn.env, invokes subprocess

5. Spawned `claude` reads creds from profile_dir, commits as alice via
   GIT_CONFIG_GLOBAL, makes API calls billed to alice's subscription.
```

### tmux env capture

On `devbrain login`, the wizard sets a tmux session env var so the dev's submissions auto-attribute:

```
tmux setenv -t alice DEVBRAIN_DEV_ID alice
```

The `devbrain factory submit` CLI defaults `submitted_by` to `$DEVBRAIN_DEV_ID` if unset, so devs don't have to remember to pass `--dev`.

### Login flow

```
1. profiles.get_profile_dir(dev_id) → ensures dir, validates id
2. First-run identity prompt → write .gitconfig
3. adapter.login(dev, profile_dir):
   - Pre-flight: verify_reverse_tunnel(adapter.oauth_callback_ports)
   - Invoke CLI's native login with env-isolated to profile_dir
   - Verify creds landed (.claude.json or equivalent)
4. Echo success
```

### Logins status flow

```
For each profile in profiles.list_profiles():
  For each adapter in registry.all():
    Print profile.dev_id, adapter.name, adapter.is_logged_in(...) → table
```

## Authentication & SSH-OAuth Callback

OAuth round-trips need to terminate somewhere both the CLI (on Mac Studio) and the dev's browser (on laptop) can reach.

**Behavioral probe findings (2026-04-29) confirmed no reverse SSH tunnel is required for any of the three CLIs:**

| CLI | Mechanism | Why no tunnel |
|---|---|---|
| **Codex** | `--device-auth` flag | Device-code flow — CLI prints URL + short code, no localhost callback. The adapter passes `--device-auth` automatically. |
| **Claude** | Hosted callback | OAuth redirect goes to `https://platform.claude.com/oauth/code/callback` (verified in probe output). No localhost listener bound. |
| **Gemini** | API-key fallback | If `dev.gemini_api_key` is set, OAuth is skipped entirely — `GEMINI_API_KEY` env var is enough. The OAuth path itself has not been exhaustively probed; the API-key path is the recommended default for SSH sessions. |

**SSH config template** (in onboarding doc) — only one `RemoteForward` line, for browser driving via PKRelay:
```sshconfig
Host mac-studio
  HostName lhts-mac-studio.local
  User lhtdev
  IdentityFile ~/.ssh/<dev_key>
  RemoteForward 18794 localhost:18793   # PKRelay browser tunnel
```

**Adapter responsibility:** each adapter's `login()` selects the right flow internally — no SSH-config tuning needed for the operator. See `factory/ai_clis/PROBE_NOTES.md` for the full per-CLI probe results.

## Error Handling

| Scenario | Response |
|---|---|
| Profile missing when factory job spawns | Job → `needs_human`, message: *"Run `devbrain login --dev <id> --cli <name>`"* |
| OAuth expired mid-job | `is_logged_in` returns false → `needs_human`, recovery message |
| Dev not registered | Reject in `profiles.get_profile_dir()`: *"Run `devbrain register --dev <id>` first"* |
| Concurrent jobs same dev | Serialize AI-CLI phases per-dev (orchestrator already does this); non-AI work parallel-safe |
| Path traversal via dev_id | `validate_dev_id()` regex `[a-z0-9_-]{1,64}` at every entry point |
| Symlink rot for shared dotfiles | `devbrain login --refresh-symlinks` recreates from `factory.shared_dotfiles` config |
| OAuth flow inside SSH session | Reverse tunnel for callback; URL displayed, dev pastes into laptop browser |

## Testing

**Unit tests per adapter:**
- `spawn_args` returns expected env dict (HOME / CODEX_HOME, GIT_CONFIG_GLOBAL, git author env vars)
- `is_logged_in` detects expected files
- `login` mocked for pre-flight/error paths

**Integration tests:**
- Submit job with `submitted_by="alice"` → mocked CLI binary dumps env to stdout → assert env matches adapter's spawn_args
- Missing profile → job → `needs_human` with right message

**Adversarial:**
- `validate_dev_id("../etc/passwd")` rejects
- `validate_dev_id("alice")` accepts

**Smoke test (acceptance gate, manual):**
- Patrick acts as fresh dev: `devbrain login --dev patrickkelly-test --cli claude` → walk through OAuth → factory submit trivial job → confirm via `devbrain logs` the spawned `claude` had `HOME=~/devbrain/profiles/patrickkelly-test`. Concrete success: commit lands with the test profile's git identity, not `lhtdev`.

## Onboarding Doc (Model 1) — `docs/ONBOARDING_TEAMMATE.md`

Replaces the deleted Model-2 doc. Covers:

1. Architecture diagram (laptop ↔ Mac Studio with reverse tunnels labeled)
2. Prereqs (Mac, SSH key, PKRelay extension on laptop)
3. Patrick adds dev's pubkey to `lhtdev@mac-studio`
4. Dev's full SSH config block (RemoteForward entries for PKRelay + OAuth callback ports)
5. SSH in, create persistent tmux session
6. `devbrain register --dev <id> --name "..." --channel tmux:<id>`
7. `devbrain login --dev <id> --cli claude` then `--cli codex`, `--cli gemini` (includes git identity prompt)
8. `devbrain logins --dev <id>` to verify; submit hello-world factory job
9. Day-2 ops (re-login on expiry, offboarding, tmux session conventions)
10. Troubleshooting (OAuth callback hang, missing profile, factory stuck in `needs_human`)

## Out of Scope (v1)

- Per-job profile dirs (would require re-doing OAuth per job — not viable)
- Custom OAuth client implementations (we orchestrate, vendors implement)
- Token refresh logic (CLIs manage their own)
- TUI dashboard notifications panel (separate follow-up; tmux:popup channel already works)
- Browser-driving from factory orchestrator (PKRelay reverse-tunnel pattern already documented in `pkrelay/docs/REMOTE-SETUP.md`)

## Implementation Plan Pointer

This design is the input to a forthcoming implementation plan (use `superpowers:writing-plans`). Estimated scope: ~1 day per the inventory estimate, broken into:

1. Behavioral probe of each AI CLI's OAuth callback mechanism (1 hour)
2. `factory/ai_clis/` module + base + 3 adapters with unit tests (3-4 hours)
3. `factory/profiles.py` + path validation + symlink management (1 hour)
4. `cli.py` `login`/`logins`/`logout` commands (2 hours)
5. `cli_executor.run_cli` integration + integration tests (1-2 hours)
6. Onboarding doc rewrite (1 hour)
7. Smoke test as acceptance gate (manual, 30 min)

## Open Questions

1. Each AI CLI's actual OAuth callback port — fixed or ephemeral? Resolved by behavioral probe before coding.
2. Whether Claude reads OAuth from keychain in addition to `~/.claude.json` — if it falls through to keychain when the file is missing, HOME-swap won't isolate. Resolved by behavioral test.
3. Should `setup-multi-dev` (the existing PR #49 wizard) be retired since it's Model-2 oriented? Recommend yes after this lands; it's currently vestigial. Track as separate cleanup task post-implementation.
