<!--
Thanks for contributing to DevBrain! A short PR template — fill in what
applies, delete what doesn't.
-->

## Summary

<!-- 1-3 sentences: what changes, why, and the user-visible effect. -->

## Test plan

- [ ] `pytest factory/tests/` passes locally
- [ ] Lint clean (no new warnings)
- [ ] If this PR touches DB schema or migrations: applied + rolled back cleanly against a scratch DB

## LLM call path

This section applies to any PR that touches:
  - `factory/cognify/_anthropic_auth.py` or `factory/cognify/extract.py` or `factory/cognify/edges.py`
  - `factory/ai_clis/` (Claude/Codex/Gemini adapters)
  - `mcp-server/src/index.ts` if it adds or modifies API calls

Pick one:

- [ ] **This PR does NOT touch any LLM call path** — skip the rest of this section.
- [ ] **This PR touches an LLM call path and I have verified end-to-end against a real Anthropic API call.** Evidence (paste one):
  - [ ] CI `llm-smoke-test` workflow ran and passed on this PR (preferred). Workflow run: <!-- paste URL -->
  - [ ] I ran a local smoke test. Request ID / response status: <!-- paste -->
  - [ ] I added the `llm-smoke-test` label to this PR to opt-in to the CI smoke test (defaults to "paths-filter only" — labeling forces a run).

Why this section exists: PR #119 shipped subscription OAuth support that 429'd on every real call because the system-prompt fingerprint was missing. The bug survived code review because no test exercised the live API path. This checkbox is the lightest-weight defense.
