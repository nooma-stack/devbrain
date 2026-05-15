# DevBrain Roadmap Status — 2026-05-15

> Snapshot of where DevBrain is against its 8-phase roadmap, what's
> still in flight, what's explicitly deferred, and a "production
> ready" assessment by audience. Compiled after the data-hygiene
> sprint that landed migrations 032–036.

---

## 1. 8-phase roadmap status

The original roadmap was laid out in
[`docs/plans/2026-04-15-hardening-plan.md`](2026-04-15-hardening-plan.md)
and progressively expanded through Atlas Steps 1–10 (see PRs #67–#104
on `nooma-stack/devbrain`).

| Phase | Scope | Status | Reference |
|---|---|---|---|
| **0** | Hardening — installable from the repo alone | ✅ Done (Apr 2026) | hardening plan |
| **1** | First real instance (Mac Studio) + operational isolation feedback | ⚠️ Partial | onboarding runbook |
| **2** | Unified memory model — collapse `decisions`/`patterns`/`issues` into `devbrain.memory` | ✅ Done | migration 010, dual-write helpers (PRs #42-48) |
| **3** | Discipline layer — curator, eval agents, rule engine, audit ledger | ✅ Done | Atlas Steps 1–7 (PRs #68-90) |
| **4** | Codex + Gemini adapters + per-CLI config | ✅ Done | commit `f00eb07` |
| **5** | Graph layer — `memory_dependencies` + multi-hop walker | ✅ Done | PR #97 (recursive CTEs, Apache AGE rejected after re-eval) |
| **6** | Cognify/Memify pipeline split | ✅ Done | PR #98 (`factory/cognify/`) |
| **7+** | Ops polish (spend, versioned re-extraction, dashboard, multi-CLI) | ⚠️ Partial | see §2 |
| **8** | Cross-project fan-out — `cognify_fanout` pass writing per-project summaries with `derived_from` edges | 📋 Proposed | PR #121 design doc, **no implementation** |

---

## 2. Phase 7+ ops-polish breakdown

| Item | Status | Notes |
|---|---|---|
| Per-call LLM spend tracking + daily view | ✅ Done | PR #103 (migration 029) |
| Versioned re-extraction (`cognify-reextract --since-version=N`) | ✅ Done | migration 030 + PR #103 |
| Cognify status dashboard panel | ✅ Done | PR #134 |
| Active-dev-sessions dashboard panel | 🚫 Blocked | Issue #135 — needs `dev_id` + `cli` columns in `raw_sessions` (schema change) |
| Multi-CLI invitation kit (one kit onboards claude + codex + gemini) | 📋 Open | PR #131, 6 design questions need answers |
| Cross-project cognify flag | ⚠️ Half | dev ACLs landed in PR #104; the flag itself was the deferred half |

---

## 3. Data-hygiene sprint (2026-05-14 → 2026-05-15)

Single-day sprint that landed before the roadmap snapshot was taken.
Not on the original roadmap but materially raises production-readiness
floor.

| PR | Migration | What it did |
|---|---|---|
| #140 | 032 | Fixed `memory.provenance_id` to point at the real session UUID (was the chunk's own UUID — broke session-grouped atomization). Backfilled 47K+ rows. Rewrote `cognify-bulk` discover queries to JOIN `raw_sessions` so `--since` filters on real conversation date. |
| #141 | 033 + 034 | Cleaned ~7.5M test-pollution memory rows + ~6.6M rule-pollution archived rows. Pruned 14.2M orphan ledger entries and rebuilt the audit chain so `verify_chain()` passes. DB 12 GB → 2.4 GB. |
| #142 | 035 | Dedupped 5 seeded compliance rules from 120 active copies → 5. |
| #143 | 036 | Reconstructed 296 orphan brightbot sessions with real conversation timestamps. Deleted 92 duplicate orphan chunks (verified strict subsets of survivors). Added `delete_session()` helper so the 2026-04-09 orphaning bug can't recur. |

**Net result on brightbot**: 455 → 751 atomizable sessions, zero orphans,
all linked correctly, real timestamps available for `--since` filtering.

---

## 4. Explicitly deferred from v0.1 (production-blocking depending on use case)

| Item | Why it matters |
|---|---|
| **Multi-instance operational isolation** (separate DB / schema per instance) | Today, BrightBot + DevBrain + LHT-VPS + PKRelay all share one DB namespace per host. Production isolation requires per-instance segmentation. |
| **Cross-platform support** (Linux server, Windows dev) | macOS-only. Limits where Mac Studio's clients can run. Mike Courtney's Windows onboarding hit PCMatic + Git-Bash issues directly tied to this gap. |
| **Encryption at rest for credentials** | OAuth tokens, Postgres passwords, etc. are written to disk in plaintext. HIPAA-adjacent risk. |
| **Retry logic / Ollama fallback** | Single Ollama instance is a SPOF for embeddings + summarization. |

---

## 5. Items not on the original roadmap that we'd add

### Immediate (next session candidates)

| Item | Owner | Notes |
|---|---|---|
| **Mac Studio chain repair** | (devbrain) | One-shot data sync from laptop's clean state. Mac Studio's `chunks.source_id` values don't match its `raw_sessions.id` values — broken since an earlier laptop→Mac-Studio export/import. Migrations 033/035 will run there but #036 reconstruction won't apply (different pattern). |
| **BrightBrain MCP completion** | (brightbrain) | Per 2026-05-15 working note, the brightbrain MCP isn't fully implemented. BrightBot's CLAUDE.md routes there but should currently use the local devbrain. Either complete brightbrain or update BrightBot's routing. |
| **Issue #126** | claude.py adapter | OAuth token written to disk but `invitations.oauth_token_received_at` never updated in DB. Mike Courtney's onboarding row stays `status=activated` with NULL timestamp. |
| **Cognify-bulk on brightbot's 751 sessions** | operational | The atomization run we paused at the end of 2026-05-15. Single laptop run, ~$10-30, hours. |

### Medium term (production-readiness layer)

| Item | Why |
|---|---|
| **Backup/restore tooling beyond `pg_dump`** | The 2026-04-09 orphan-chunks incident was a near-miss. A consistent point-in-time snapshot + verified-restore tool would have caught the data drift sooner. |
| **Search UI / admin web app for memory** | Currently MCP-only. The Textual TUI dashboard is factory-jobs-only. No human-browseable interface for the memory itself. |
| **PHI/PII redaction at ingest** | The rule engine flags PHI-handling code in atoms (FERPA/HIPAA rules), but `raw_sessions.raw_content` stores transcripts unredacted. Inverts the protection ordering for regulated workloads. |
| **Metrics / observability export** | Prometheus / OpenTelemetry hooks. Currently no production monitoring surface. |

### Longer term

| Item | Why |
|---|---|
| **Cross-project fan-out implementation (Phase 8)** | PR #121 design exists; not started. Would reduce same-info-stored-N-times pattern for cross-project work. |
| **Multi-region replication / HA** | Pure availability play. Not needed until Mac Studio is no longer the only authoritative instance. |
| **Per-project RBAC for memory access** | Today, anyone with MCP access sees all projects they're not explicitly ACL-blocked from. Inverts to a deny-by-default model when external collaborators arrive. |

---

## 6. "Production ready" by audience

| Audience | Status | Biggest gap |
|---|---|---|
| **You (solo, laptop)** | ✅ Ready | Cognify-bulk on the 751 brightbot sessions hasn't been run yet |
| **You + 2-3 trusted devs (multi-dev local)** | ⚠️ Mostly ready | Multi-CLI kit (PR #131), Issue #126, Mac Studio repair |
| **External team / SaaS / regulated workload** | 🚫 Not ready | Multi-instance isolation, encryption at rest, RBAC, PHI redaction, BCP/DR |
| **BrightBot prod MCP integration (immediate use case)** | ✅ Workable | Decide laptop-via-tunnel vs Mac-Studio-after-repair |

---

## 7. Decision points before continuing

Before the next major push of work, the following architectural
decisions are worth pinning down so subsequent PRs don't get redone.

1. **Mac Studio's role.** Source-of-truth for shared memory, or read-only
   replica of laptop? Today's broken chain has to be repaired either way,
   but the *direction* of future sync (laptop → MS one-time, vs ongoing
   replication) shifts the design.

2. **Multi-instance isolation timing.** Currently every project lives in
   the same `devbrain` schema. If BrightBot prod is going to write to
   devbrain at production volume, per-project schema isolation should
   land before — not after — that traffic ramps.

3. **PHI/PII redaction policy.** Especially BrightBot — student data, IEPs,
   PHI. Today's rules surface in agent briefings, but the raw transcripts
   are stored plaintext. For HIPAA-relevant projects this should probably
   be solved before bulk-atomization makes the transcripts more
   discoverable via vector search.

4. **Phase 8 fan-out timing.** Patrick's existing PR #121 design proposes
   `cognify_fanout` for cross-project summaries. Worth shipping before or
   after the broader multi-tenancy / encryption work? (Phase 8 reduces the
   surface area where the same info lives in multiple places, which is
   helpful for RBAC.)

---

## 8. What this doc is NOT

- **Not a commitment**: items here are options for prioritization, not
  promised deliverables.
- **Not exhaustive**: small QoL fixes (e.g. CLAUDE.md routing notes,
  individual flaky tests) aren't itemized.
- **Not a substitute for the issue tracker**: open issues + PRs on
  `nooma-stack/devbrain` are the authoritative work queue.
- **Not historical**: this is a 2026-05-15 snapshot. Re-run the
  assessment after the next major milestone (likely the multi-CLI kit
  + Mac Studio repair) before relying on it for planning.
