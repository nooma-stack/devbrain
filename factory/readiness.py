"""Factory readiness: pre-job and post-job verification that the working
tree, git HEAD, and lock table are in a clean state to start (or finish)
a job.

Two checkpoints bracket every job:

1. Pre-job (orchestrator entry, before the first planning transition):
   verify → auto-repair → verify again. If remaining issues, transition
   the job to BLOCKED with block_reason="factory_not_ready" and set the
   factory_runtime_state.not_ready flag so subsequent jobs block fast
   instead of inheriting the mess.

2. Post-job (cleanup_agent, after the normal post-cleanup report):
   verify → auto-repair → verify again. If remaining issues, log + fire
   a needs_human notification + set the not_ready flag. The current
   job's terminal state is NOT changed (its work is done; cleanup is
   what's broken), but future jobs will block until the flag is cleared.

Both checkpoints call the same `ensure_ready()` method so the set of
checks + repair actions is defined once.

Motivation: observed 2026-04-22 on Mac Studio. A failed implementation
left working-tree edits referencing a schema column that didn't exist.
cleanup_agent logged "Cleaned up branch" but neither switched HEAD back
to main nor reset the working tree. The next factory job forked from
that broken state and crashed in planning. These readiness checks make
such contamination impossible to propagate silently.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from typing import Iterable

from state_machine import FactoryDB

logger = logging.getLogger(__name__)


@dataclass
class ReadinessIssue:
    """A single concrete reason the factory is not ready."""
    kind: str                    # "head_not_main" | "behind_origin" | "dirty_working_tree" | "orphan_lock"
    message: str                 # short human-readable summary
    auto_repairable: bool = True
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "message": self.message,
            "auto_repairable": self.auto_repairable,
            "details": self.details,
        }


class FactoryReadiness:
    """Verify + repair + persist factory readiness state.

    Lifecycle:
        readiness = FactoryReadiness(db, project_root)
        remaining = readiness.ensure_ready()
        if remaining:
            # block the current job — factory is contaminated
            ...
    """

    def __init__(self, db: FactoryDB, project_root: str, base_branch: str = "main"):
        self.db = db
        self.project_root = project_root
        self.base_branch = base_branch

    # ─── Public API ───────────────────────────────────────────────────

    def verify(self) -> list[ReadinessIssue]:
        """Return the current list of readiness issues (empty = ready).

        Ordering: head_not_main → behind_origin → dirty_working_tree →
        orphan_lock. This reads as "checkout main → sync with origin →
        reset working tree → release orphan locks", which is also the
        logical order of the repair actions.
        """
        issues: list[ReadinessIssue] = []
        issues.extend(self._check_head_on_base())
        issues.extend(self._check_behind_origin())
        issues.extend(self._check_dirty_working_tree())
        issues.extend(self._check_orphan_locks())
        return issues

    def attempt_repair(self, issues: Iterable[ReadinessIssue]) -> None:
        """Best-effort auto-repair for the given issues. Safe to call
        repeatedly; each repair is idempotent. Exceptions are caught
        per-issue so a single bad repair doesn't abort the rest.
        """
        for issue in issues:
            if not issue.auto_repairable:
                continue
            try:
                if issue.kind == "head_not_main":
                    self._repair_head()
                elif issue.kind == "behind_origin":
                    self._repair_working_tree()
                elif issue.kind == "dirty_working_tree":
                    self._repair_working_tree()
                elif issue.kind == "orphan_lock":
                    self._repair_orphan_locks()
                else:
                    logger.warning("Unknown readiness issue kind: %s", issue.kind)
            except Exception as exc:  # noqa: BLE001 — best-effort path
                logger.warning("Repair of %s failed: %s", issue.kind, exc)

    def ensure_ready(self) -> list[ReadinessIssue]:
        """Fetch-origin → verify → repair → verify. Returns the list of
        issues that could not be auto-repaired (empty list means the
        factory is ready). Side effect: persists or clears the not_ready
        flag in devbrain.factory_runtime_state to match the final state.

        The fetch is best-effort and informational — its return value is
        deliberately ignored so an offline factory-host still runs local
        code. The behind_origin check that follows will simply report 0
        commits behind if the fetch couldn't refresh origin refs.
        """
        self._fetch_origin()
        issues = self.verify()
        if not issues:
            self._clear_flag()
            return []

        self.attempt_repair(issues)
        remaining = self.verify()

        if remaining:
            self._set_flag(remaining)
        else:
            self._clear_flag()
        return remaining

    # ─── Flag persistence ─────────────────────────────────────────────
    # The factory_runtime_state table is a singleton-style KV store.
    # Presence of a row with key='not_ready' is the flag; its reasons
    # jsonb carries the diagnostic payload for UIs / factory_status.

    def get_flag(self) -> dict | None:
        """Read the persisted not_ready flag, if any."""
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT reasons, updated_at FROM devbrain.factory_runtime_state "
                "WHERE key = 'not_ready' LIMIT 1"
            )
            row = cur.fetchone()
        if not row:
            return None
        return {"reasons": row[0], "updated_at": row[1].isoformat()}

    def _set_flag(self, issues: list[ReadinessIssue]) -> None:
        payload = json.dumps([i.to_dict() for i in issues])
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO devbrain.factory_runtime_state (key, reasons, updated_at)
                VALUES ('not_ready', %s::jsonb, NOW())
                ON CONFLICT (key) DO UPDATE
                  SET reasons = EXCLUDED.reasons, updated_at = NOW()
                """,
                (payload,),
            )
            conn.commit()
        logger.warning(
            "Factory marked NOT READY with %d unresolved issue(s): %s",
            len(issues),
            "; ".join(i.message for i in issues),
        )

    def _clear_flag(self) -> None:
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM devbrain.factory_runtime_state WHERE key = 'not_ready'"
            )
            conn.commit()

    # ─── Checks ───────────────────────────────────────────────────────

    def _check_head_on_base(self) -> list[ReadinessIssue]:
        head = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
        if head is not None and head != self.base_branch:
            return [ReadinessIssue(
                kind="head_not_main",
                message=f"HEAD is on '{head}' (expected '{self.base_branch}')",
                details={"current_head": head, "expected": self.base_branch},
            )]
        return []

    def _check_behind_origin(self) -> list[ReadinessIssue]:
        """Emit a behind_origin issue if HEAD is behind origin/<base_branch>.
        Fail-safe: any parse error or git failure returns [] (no false
        alarm). Runs AFTER _fetch_origin() at the ensure_ready() entry
        point so the comparison is against freshly-fetched origin refs.
        """
        out = self._run_git(
            ["rev-list", "--count", f"HEAD..origin/{self.base_branch}"],
            allow_fail=True,
        )
        if out is None:
            return []
        try:
            count = int(out.strip())
        except ValueError:
            return []
        if count <= 0:
            return []
        return [ReadinessIssue(
            kind="behind_origin",
            message=f"HEAD is {count} commit(s) behind origin/{self.base_branch}",
            details={"commits_behind": count},
        )]

    def _check_dirty_working_tree(self) -> list[ReadinessIssue]:
        # Working tree must be clean (no uncommitted modifications, no
        # untracked files). We cap entries in the reason payload so a
        # very dirty tree doesn't explode the metadata blob.
        porcelain = self._run_git(["status", "--porcelain"])
        if not porcelain:
            return []
        lines = [ln for ln in porcelain.split("\n") if ln.strip()]
        if not lines:
            return []
        return [ReadinessIssue(
            kind="dirty_working_tree",
            message=f"Working tree has {len(lines)} dirty/untracked entries",
            details={"entries": lines[:20], "truncated": len(lines) > 20},
        )]

    def _check_orphan_locks(self) -> list[ReadinessIssue]:
        """File locks whose owning job is in a terminal state and should
        have had its locks released. These stack up when cleanup paths
        fail and block future jobs from acquiring the same files.
        """
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT fl.id, fl.job_id, fl.file_path, fj.status
                FROM devbrain.file_locks fl
                JOIN devbrain.factory_jobs fj ON fj.id = fl.job_id
                WHERE fj.status IN ('approved', 'rejected', 'deployed', 'failed')
                LIMIT 50
                """
            )
            rows = cur.fetchall()
        if not rows:
            return []
        sample = [{"job_id": r[1][:8], "file_path": r[2], "job_status": r[3]} for r in rows[:5]]
        return [ReadinessIssue(
            kind="orphan_lock",
            message=f"{len(rows)} file lock(s) held by terminal jobs",
            details={"count": len(rows), "sample": sample, "truncated": len(rows) >= 50},
        )]

    # ─── Sync ─────────────────────────────────────────────────────────

    def _fetch_origin(self) -> bool:
        """Best-effort `git fetch origin <base_branch>` at the start of
        ensure_ready(). Returns True on success, False on any failure
        (timeout, git missing, non-zero exit). NEVER raises and NEVER
        emits a readiness issue — an offline factory-host must still be
        able to run local code. The subsequent behind_origin check will
        simply report 0 commits behind if origin couldn't be refreshed.
        """
        try:
            result = subprocess.run(
                ["git", "fetch", "origin", self.base_branch],
                cwd=self.project_root,
                capture_output=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(
                "git fetch origin %s failed (exception): %s",
                self.base_branch, exc,
            )
            return False
        if result.returncode != 0:
            logger.warning(
                "git fetch origin %s returned %d: %s",
                self.base_branch,
                result.returncode,
                result.stderr.decode("utf-8", "replace").strip(),
            )
            return False
        logger.info("Fetched origin/%s", self.base_branch)
        return True

    # ─── Repair actions ───────────────────────────────────────────────

    def _repair_head(self) -> None:
        """Switch back to base_branch. Uses -f so uncommitted changes
        (which we're about to wipe in _repair_working_tree anyway) don't
        block the checkout. Safe here because the factory's assumption
        is it's the *only* writer of the repo."""
        self._run_git(["checkout", "-f", self.base_branch], allow_fail=True)

    def _repair_working_tree(self) -> None:
        """Hard reset + clean. Discards ALL local changes.

        This is aggressive but correct in context: the factory is the
        sole writer of the repo, and any dirtiness is always a factory
        bug (a cleanup step that didn't run, a crashed implementer that
        didn't rollback, etc.). There is no user-facing work to
        preserve at this scope.
        """
        self._run_git(["reset", "--hard", f"origin/{self.base_branch}"], allow_fail=True)
        self._run_git(["clean", "-fd"], allow_fail=True)

    def _repair_orphan_locks(self) -> None:
        """Release every file lock whose owning job is in a terminal
        state. Uses a single DELETE so it's atomic."""
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM devbrain.file_locks
                WHERE job_id IN (
                    SELECT id FROM devbrain.factory_jobs
                    WHERE status IN ('approved', 'rejected', 'deployed', 'failed')
                )
                """
            )
            released = cur.rowcount
            conn.commit()
        if released:
            logger.info("Released %d orphan file lock(s)", released)

    # ─── Internals ────────────────────────────────────────────────────

    def _run_git(self, args: list[str], *, allow_fail: bool = False) -> str | None:
        """Run `git <args>` in project_root. Returns stripped stdout on
        success, or None on failure. When allow_fail=False (default),
        non-zero return logs a warning; when True, failures are silent
        (used by the repair steps where the underlying state may be
        unusual)."""
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.project_root,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            if not allow_fail:
                logger.warning("git %s timed out or not found: %s", " ".join(args), exc)
            return None
        if result.returncode != 0:
            if not allow_fail:
                logger.warning(
                    "git %s failed (%d): %s",
                    " ".join(args), result.returncode, result.stderr.strip(),
                )
            return None
        return result.stdout.strip()
