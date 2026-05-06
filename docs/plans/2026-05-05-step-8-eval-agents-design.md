# Atlas Step 8+ — Additional Eval Agents (`eval_perf` + `eval_lint`)

> **Status:** Design locked, ready for implementation plan.
>
> **Scope:** Add two eval agents to the existing eval chain (Step 6b).
> `eval_perf` (LLM-driven, joins the cached eval chain). `eval_lint`
> (subprocess wrapper around ruff/eslint, runs separately).
>
> **Non-goals:** New eval agents beyond these two. `eval_hipaa` is
> permanently dropped (dissolved into Phase 7 profile-tagged rules,
> shipped). `eval_a11y`, `eval_doc`, etc. are deferred unless concrete
> demand surfaces.
>
> **Verification gate:** new postulates pass + 2 new agents runnable via
> `run_evals()` orchestration + all prior postulates still pass.

---

## 1. Drivers

The eval chain shipped in Step 6b runs two LLM agents (`eval_security`,
`eval_test`) sequentially with prompt caching at the IMPLEMENTING →
REVIEWING transition. Two gaps:

1. **Perf antipatterns aren't caught.** A factory job that introduces an
   N+1 query, an unbounded SELECT, or a missing index will pass
   eval_security + eval_test but degrade prod runtime. `eval_perf` is the
   pattern detector for this class.

2. **Lint isn't run programmatically.** Today's pre-commit hooks catch
   ruff/eslint locally but the dev factory's eval phase doesn't replay them
   against the diff. A subprocess wrapper that runs the project's own ruff
   + eslint config and surfaces findings as eval artifacts closes that gap.

Both are additive — they don't change existing eval semantics.

## 2. Locked decisions

| # | Decision |
|---|---|
| 1 | **Two new agents:** `eval_perf` (LLM-driven) + `eval_lint` (subprocess-driven). Step 8 ships `eval_perf`; Step 9 ships `eval_lint` |
| 2 | `eval_perf` joins the **LLM-cached eval chain** (security → test → perf). Same `brief + plan + diff` context, hits warm prompt cache from prior agents — ~10× cost reduction |
| 3 | `eval_lint` runs **independently of the LLM chain.** Pure subprocess wrapper around `ruff` (Python) and `eslint` (JS/TS). Zero LLM cost. Output mapped to `EvalFinding` for consistency |
| 4 | Both agents persist to existing `devbrain.factory_artifacts` (`phase='reviewing'`, `artifact_type=agent_name`) — same pattern as eval_security/eval_test |
| 5 | Failure isolation — if `eval_perf` errors, `eval_lint` still runs; vice versa |
| 6 | `eval_perf` v1: **LLM-only pattern detection**. AST-based static analysis is Phase 8.x optimization once we know which patterns dominate the false-positive list |
| 7 | `eval_lint` autodetects project config: `pyproject.toml [tool.ruff]` / `ruff.toml` for Python; `.eslintrc.*` / `eslint.config.js` for JS-TS. Uses project's own config — does NOT impose DevBrain-specific rules |
| 8 | Skip eval_lint when project has no detectable config (no false-positive flood from running tools with default rules) |

## 3. The two agents

### 3.1 `eval_perf` — LLM-driven perf antipattern detection

**File:** `factory/curator/eval/eval_perf.py` + `factory/curator/eval/prompts/eval_perf.md`

**Detects (v1 prompt scope):**
- N+1 query patterns (SELECT inside loops; ORM `.each()`/`.map()` with embedded queries)
- Unbounded SELECT (missing LIMIT on user-driven queries)
- Missing indexes (joins on non-indexed columns; large WHERE-clause filters on non-indexed columns)
- Synchronous I/O in async contexts (Python `requests` in async handlers; node sync fs ops in handlers)
- O(N²) array operations on user-driven inputs

**Out of scope:** runtime profiling, query plan analysis, real-instance EXPLAIN
output ingestion. Those are Phase 8.x or runtime APM territory.

**Order in eval chain:**
```
eval_security  →  eval_test  →  eval_perf
(primes cache)    (warm)         (warm)
```

`eval_perf` is third because it has the lowest cost-of-failure; if cache
warming somehow degrades it, security + test still ran cleanly first.

**Cost target:** ~10% of cold call (cache hit on shared `brief + plan +
diff` context). Per-call timeout: 60s (matches existing).

### 3.2 `eval_lint` — subprocess wrapper

**File:** `factory/curator/eval/eval_lint.py`

**Behavior:**
1. Detect project type (Python via pyproject.toml, JS/TS via package.json).
2. For Python: run `ruff check --output-format=json <touched_files>`.
3. For JS/TS: run `npx eslint --format=json <touched_files>`.
4. Parse JSON output, map to `EvalFinding`:
   - `rule_id` → ruff/eslint rule code (e.g., `E501`, `no-unused-vars`)
   - `severity` → map ruff/eslint severity to (critical | important | minor)
   - `file` + `line` → from output
   - `message` → linter's message
   - `fix_hint` → suggested fix from output if available
5. Persist to factory_artifacts with `artifact_type='eval_lint'`.

**Files passed:** the diff's touched files only (avoid re-linting the whole
project; tightens to what the factory job actually changed).

**Skip conditions:**
- No detectable config → skip with `EvalResult(findings=[], skipped="no config")`.
- Linter binary not in PATH → skip with `EvalResult(findings=[], skipped="ruff not installed")`.
- Tool returns non-zero on internal error (not lint findings) → log + skip
  (don't fail the factory job because the linter crashed).

**Cost:** zero LLM. Subprocess elapsed time typically <5s for small diffs.

**Runs independently** of the LLM chain — same state machine hook
(`IMPLEMENTING → REVIEWING`) but no cache to coordinate. Can run in parallel
with the LLM chain in a future optimization (out of scope for v1).

## 4. Integration with existing runner

**Modify `factory/curator/eval/runner.py`:**

```python
# Existing _AGENT_ORDER:
_LLM_AGENT_ORDER: list[tuple[str, str]] = [
    ("eval_security", "eval_security.md"),
    ("eval_test", "eval_test.md"),
    ("eval_perf", "eval_perf.md"),  # NEW
]

# New separate runner for non-LLM evals:
def run_static_evals(conn, job_id, diff_files) -> list[EvalResult]:
    """Run eval_lint (subprocess-driven). Independent of LLM cache chain."""
    return [eval_lint.run(conn, job_id, diff_files)]


def run_all_evals(conn, job_id, brief, plan, diff, diff_files) -> list[EvalResult]:
    """Top-level orchestrator. Calls both LLM chain and static chain.
    Returns combined results in eval_security, eval_test, eval_perf,
    eval_lint order."""
    llm_results = run_evals(conn, job_id, brief, plan, diff)
    static_results = run_static_evals(conn, job_id, diff_files)
    return llm_results + static_results
```

State machine wiring (`factory/state_machine.py`) calls `run_all_evals()`
instead of `run_evals()`. One-line change.

## 5. Postulates + verification

**New postulates:**

| Postulate | Asserts |
|---|---|
| `P_eval_perf_warm_cache` | `eval_perf` invocation cost is <20% of cold cost when run after eval_security + eval_test (validates cache hit) |
| `P_eval_perf_failure_isolation` | If `eval_perf` errors, `eval_lint` still runs to completion |
| `P_eval_lint_skip_no_config` | A project with no ruff/eslint config produces an empty EvalResult with `skipped` reason, not a failure |
| `P_eval_lint_diff_scope` | `eval_lint` only runs against the touched files in the diff, not the whole project |
| `P_eval_lint_severity_mapping` | ruff `error`/`warning`/`info` map correctly to `critical`/`important`/`minor` |

**Existing tests must still pass:** all 22+7+7 prior postulates (Phase 5 +
Phase 6 add postulates), 5 curator-brief tests, 5 per-rule postulates,
existing eval_security + eval_test integration tests.

**Integration tests:**
- `factory/tests/test_eval_perf.py` — synthetic diff with N+1 → finding emitted; clean diff → empty findings
- `factory/tests/test_eval_lint.py` — synthetic diff with `f-string-without-placeholders` → finding emitted; clean diff → empty
- `factory/tests/test_runner_chain.py` — full chain (security + test + perf + lint) with all four findings types persisted

**Coverage gate:** each new module ≥ 85%.

## 6. Sub-PR sequence

```
8 (eval_perf joins the LLM cached chain)  →  9 (eval_lint as static eval)
```

Two independently mergeable PRs. Step 8 ships first because it touches
the runner's chain (smaller, less risky to land first). Step 9 adds the
parallel static-eval runner.

| Sub-PR | Title | Scope |
|---|---|---|
| 8 | `feat(eval): Atlas Step 8 — eval_perf agent joins cached eval chain` | `eval_perf.py` + prompt + runner integration + 2 postulates + integration test |
| 9 | `feat(eval): Atlas Step 9 — eval_lint subprocess wrapper for ruff + eslint` | `eval_lint.py` + `run_static_evals` orchestrator + state machine call site update + 3 postulates + integration test |

## 7. Out of scope (carried forward)

- **`eval_hipaa`, `eval_a11y`, `eval_doc`, etc.** — eval_hipaa is permanently
  dropped (dissolved into Phase 7 profile-tagged rules, already shipped).
  Other agents deferred unless concrete demand surfaces; the eval pattern
  is established and adding more is mechanical.
- **AST-based static analysis for eval_perf** — Phase 8.x optimization
  once false-positive patterns are clear from the LLM-only v1.
- **Runtime profiling / EXPLAIN output ingestion** — Phase 8.x or APM
  territory; way out of v1 scope.
- **Custom lint rule injection** — DevBrain doesn't impose lint rules.
  Projects use their own config.
- **Parallel execution of LLM and static chains** — easy optimization
  later; not v1.

## 8. References

- DevBrain decision `9287ab95` — Atlas Phase 3 complete (eval_security + eval_test live)
- `factory/curator/eval/runner.py` — existing eval chain orchestrator (Step 6b)
- `factory/curator/eval/eval_security.py`, `eval_test.py` — pattern to mirror
- `docs/plans/2026-05-04-step-6-eval-graduation-design.md` — Step 6 eval substrate design
- DevBrain decision (Phase 7c playbook §5) — `eval_hipaa` permanently dropped, dissolved into compliance profile-tagged rules
