# AI CLI OAuth Behavioral Probe Notes

**Date:** 2026-04-29
**Probed CLIs:** claude (Claude Code 2.1.121), codex (codex-cli 0.111.0), gemini (Google Gemini CLI 0.30.0)
**Probed on:** macOS 26.4.1 arm64

These notes document the actual OAuth flow each CLI uses, gathered via
behavioral probes against real CLI binaries. The findings drive each
adapter's `spawn_args()` and `login()` strategy.

---

## Codex (`codex login`)

**Mechanism:** localhost callback (default) OR device-code flow (`--device-auth`)

**Default flow:** binds `localhost:1455` for OAuth callback. Output:

```
Starting local login server on http://localhost:1455.
If your browser did not open, navigate to this URL to authenticate:
https://auth.openai.com/oauth/authorize?...&redirect_uri=http://localhost:1455/auth/callback
```

**Headless flow:** `codex login --device-auth` skips the localhost listener
entirely. User reads a URL, opens in any browser, gets a code, types it
back into the terminal. **This is the right path for SSH sessions**
(devs SSH'd into Mac Studio cannot have callbacks land on the Mac
Studio's localhost without a reverse tunnel; device-code avoids the
problem entirely).

**Profile dir:** controlled by `CODEX_HOME` env var (verified — codex
emits `WARNING: CODEX_HOME points to "...", but that path does not exist`
when the dir is missing). Credentials stored under `<CODEX_HOME>/`:
`auth.json`, `config.toml`, `archived_sessions/`, etc.

**CodexAdapter strategy:**
- Spawn: set `CODEX_HOME=<profile>/.codex`. Precise; no HOME swap.
- Login: invoke `codex login --device-auth` for headless-friendly flow.
- is_logged_in: check `<profile>/.codex/auth.json` exists.

---

## Claude (`claude auth login`)

**Mechanism:** **hosted callback** at `https://platform.claude.com/oauth/code/callback`. NO localhost port bound.

Output of `HOME=/tmp/test claude auth login`:

```
Opening browser to sign in…
If the browser didn't open, visit:
https://claude.com/cai/oauth/authorize?...&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&...
```

User completes OAuth in browser → claude.com captures the code →
displays it (or auto-redirects to the local CLI somehow). The CLI then
either reads the code from a paste or polls for token via separate
mechanism. Either way: **no localhost port** is bound, so SSH reverse
tunneling is not required for the OAuth callback.

**Profile dir:** **No `CLAUDE_CONFIG_DIR` env var per official docs**
(verified via `code.claude.com/docs/en/settings`). Credentials under
`~/.claude/` and `~/.claude.json` (OAuth state). HOME-swap is the only
mechanism to redirect.

**ClaudeAdapter strategy:**
- Spawn: set `HOME=<profile>` (constrained to single subprocess
  invocation; orchestrator's HOME unaffected).
- Login: invoke `claude auth login` with `HOME=<profile>` env.
- is_logged_in: check `<profile>/.claude.json` exists with credential payload.

---

## Gemini (`gemini`)

**Mechanism:** OAuth via Google login (interactive flow on first run) OR `GEMINI_API_KEY` env var.

The Gemini CLI's first run prompts the user to choose an authentication
method. OAuth specifics (whether it binds a localhost port, whether it
supports device-code) could not be determined definitively from
behavioral probes (the CLI exits when stdin is non-TTY, and a TTY-bound
probe would consume an OAuth flow against the operator's real Google
account).

**Available auth env vars** (per official docs):
- `GEMINI_API_KEY` — direct API key (Google AI Studio)
- `GOOGLE_API_KEY` — Vertex AI
- `GOOGLE_CLOUD_PROJECT` — for Vertex AI mode
- `GOOGLE_GENAI_USE_VERTEXAI` — flag

**Profile dir:** `~/.gemini/` (verified via existing credentials at
`~/.gemini/google_accounts.json`). No documented config-dir env var,
so HOME-swap is the only mechanism (same situation as Claude).

**GeminiAdapter strategy:**
- Spawn: set `HOME=<profile>` (HOME-swap, same constraint as Claude).
- Login: invoke `gemini` interactively with `HOME=<profile>` env. If the
  flow proves to use a localhost port that doesn't tunnel cleanly, the
  fallback is API-key auth — `GEMINI_API_KEY` env var per dev. The
  adapter accepts both paths; the dev's Dev record can carry an
  optional `gemini_api_key` that, if present, makes the adapter prefer
  env-var auth over OAuth.
- is_logged_in: check `<profile>/.gemini/google_accounts.json` OR
  `dev.gemini_api_key` is set.

---

## Cross-CLI conclusions

| CLI | Profile-dir env | OAuth callback | Headless option |
|---|---|---|---|
| Codex | `CODEX_HOME` ✅ | localhost:1455 default | `--device-auth` ✅ |
| Claude | none — HOME-swap | hosted at platform.claude.com | implicit (hosted callback works regardless) |
| Gemini | none — HOME-swap | localhost (unverified port) | `GEMINI_API_KEY` env var fallback |

**Reverse SSH tunnel for OAuth callbacks: NOT REQUIRED.**

Codex's `--device-auth` flag, Claude's hosted callback, and Gemini's
API-key fallback all sidestep the SSH-tunnel-the-localhost-callback
problem the design doc anticipated. The only reverse tunnel needed for
Model 1 multi-dev operation is the existing PKRelay reverse tunnel
(port 18793/18794) for browser driving — unrelated to OAuth.

**Implication for design doc Section 5:** the `RemoteForward` lines for
OAuth ports in the SSH config template are unnecessary. The PKRelay
RemoteForward stays. The design doc should be patched accordingly
(non-blocking follow-up).
