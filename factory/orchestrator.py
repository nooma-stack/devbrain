"""Dev Factory orchestrator.

Runs the autonomous development pipeline:
queued → planning → implementing → reviewing → qa → ready_for_approval

Each phase spawns a CLI tool (claude, codex, gemini) as a subprocess.
Human approval is required before push/deploy.

Agents have full access to the repo and DevBrain MCP for persistent memory.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

from state_machine import FactoryDB, FactoryJob, JobStatus
from cli_executor import run_cli, notify_desktop, DEFAULT_CLI_ASSIGNMENTS
from learning import extract_lessons, get_review_lessons
from cleanup_agent import CleanupAgent
from file_registry import FileRegistry
from plan_parser import extract_files_from_plan

logger = logging.getLogger(__name__)

# Shared instructions appended to every agent prompt
DEVBRAIN_INSTRUCTIONS = """
## DevBrain — Persistent Memory

You have access to DevBrain via MCP tools. USE IT.

1. **Before starting**: Call `deep_search` to check for relevant past decisions, patterns, and issues
2. **During work**: Call `store` when you make architecture decisions (type="decision"), discover patterns (type="pattern"), or find bugs (type="issue")
3. **When done**: Summarize what you accomplished

Search DevBrain BEFORE assuming anything about architecture, past decisions, or implementation history.
"""


def _count_blocking(text: str) -> int:
    """Count actual BLOCKING findings — look for the marker at start of line or list item."""
    # Match patterns like "BLOCKING:", "**BLOCKING**", "1. BLOCKING:", "- BLOCKING:"
    pattern = r'(?:^|\n)\s*(?:\d+\.\s*|\*\*?|-\s*)?BLOCKING\b'
    return len(re.findall(pattern, text, re.IGNORECASE))


def _extract_blocking_items(text: str) -> list[str]:
    """Extract individual BLOCKING finding texts."""
    items = []
    # Split on BLOCKING markers and capture the text after each
    parts = re.split(r'(?:^|\n)\s*(?:\d+\.\s*|\*\*?|-\s*)?BLOCKING[:\s]*', text, flags=re.IGNORECASE)
    for part in parts[1:]:  # Skip text before first BLOCKING
        # Take text until next severity marker or end
        end = re.search(r'\n\s*(?:\d+\.\s*|\*\*?|-\s*)?(?:WARNING|NIT|BLOCKING)\b', part, re.IGNORECASE)
        item = part[:end.start()].strip() if end else part.strip()
        if item:
            items.append(item)
    return items


class FactoryOrchestrator:
    """Orchestrates the dev factory pipeline."""

    def __init__(self, database_url: str):
        self.db = FactoryDB(database_url)

    def run_job(self, job_id: str) -> FactoryJob:
        """Run a job through the full pipeline until it needs human approval or fails."""
        job = self.db.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        cleanup = CleanupAgent(self.db)
        logger.info("Starting pipeline for job %s: %s", job_id[:8], job.title)

        while job.status not in (
            JobStatus.READY_FOR_APPROVAL,
            JobStatus.APPROVED,
            JobStatus.REJECTED,
            JobStatus.DEPLOYED,
            JobStatus.FAILED,
        ):
            if job.status == JobStatus.QUEUED:
                job = self._run_planning(job)
            elif job.status == JobStatus.WAITING:
                job = self._run_waiting(job)
            elif job.status == JobStatus.IMPLEMENTING:
                job = self._run_implementation(job)
            elif job.status == JobStatus.REVIEWING:
                job = self._run_review(job)
            elif job.status == JobStatus.QA:
                job = self._run_qa(job)
            elif job.status == JobStatus.FIX_LOOP:
                if job.error_count >= job.max_retries:
                    # Attempt recovery before giving up
                    recovery_report = cleanup.attempt_recovery(job)
                    self.db.store_cleanup_report(
                        job_id=job.id,
                        report_type=recovery_report.report_type,
                        outcome=recovery_report.outcome,
                        summary=recovery_report.summary,
                        phases_traversed=recovery_report.phases_traversed,
                        artifacts_summary=recovery_report.artifacts_summary,
                        recovery_diagnosis=recovery_report.recovery_diagnosis,
                        recovery_action_taken=recovery_report.recovery_action_taken,
                        time_elapsed_seconds=recovery_report.time_elapsed_seconds,
                    )
                    if recovery_report.outcome == "recovered":
                        logger.info("Recovery succeeded for job %s, returning to IMPLEMENTING",
                                    job.id[:8])
                        job = self.db.transition(job.id, JobStatus.IMPLEMENTING)
                        continue
                    else:
                        job = self.db.transition(job.id, JobStatus.FAILED,
                                                 metadata={"failure": "max fix retries exceeded"})
                        notify_desktop("DevBrain Factory",
                                       f"Job FAILED: {job.title} (max retries, recovery attempted)")
                        break
                job = self._run_fix(job)

        if job.status == JobStatus.READY_FOR_APPROVAL:
            notify_desktop("DevBrain Factory",
                           f"Ready for review: {job.title}")

        # Extract lessons from review findings for the learning loop
        if job.status in (JobStatus.READY_FOR_APPROVAL, JobStatus.FAILED):
            try:
                lessons = extract_lessons(job.id)
                if lessons:
                    logger.info("Learning loop: extracted %d lessons from job %s",
                                len(lessons), job.id[:8])
            except Exception as e:
                logger.warning("Learning loop failed (non-blocking): %s", e)

        # Post-run cleanup for all terminal states
        if job.status in (
            JobStatus.READY_FOR_APPROVAL,
            JobStatus.APPROVED,
            JobStatus.REJECTED,
            JobStatus.DEPLOYED,
            JobStatus.FAILED,
        ):
            try:
                cleanup.run_post_cleanup(job.id)
                logger.info("Post-cleanup completed for job %s", job.id[:8])
            except Exception as e:
                logger.warning("Post-cleanup failed (non-blocking): %s", e)

        return job

    def _get_project_root(self, job: FactoryJob) -> str:
        """Get the project root path."""
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT root_path FROM devbrain.projects WHERE id = %s", (job.project_id,))
            row = cur.fetchone()
            return row[0] if row else "."

    def _get_cli(self, phase: str, job: FactoryJob) -> str:
        """Get the CLI to use for a phase."""
        return job.assigned_cli or DEFAULT_CLI_ASSIGNMENTS.get(phase, "claude")

    def _get_prior_findings(self, job: FactoryJob, artifact_type: str) -> list[str]:
        """Get BLOCKING findings from previous review rounds for convergence context."""
        artifacts = self.db.get_artifacts(job.id, phase="review")
        prior = []
        for art in artifacts:
            if art["artifact_type"] == artifact_type and art["blocking_count"] > 0:
                items = _extract_blocking_items(art["content"])
                prior.extend(items)
        return prior

    def _get_fix_history(self, job: FactoryJob) -> str:
        """Get summary of what was fixed in prior fix loops."""
        fix_artifacts = self.db.get_artifacts(job.id, phase="fix")
        if not fix_artifacts:
            return ""
        summaries = []
        for i, art in enumerate(fix_artifacts, 1):
            # Truncate each fix output to keep context manageable
            summaries.append(f"[Fix round {i}]\n{art['content'][:2000]}")
        return "\n\n".join(summaries)

    # ─── Planning Phase ────────────────────────────────────────────────────

    def _run_planning(self, job: FactoryJob) -> FactoryJob:
        """Planning phase: generate implementation plan from spec."""
        job = self.db.transition(job.id, JobStatus.PLANNING)

        # Fire job_started notification — job is out of the queue and into the pipeline
        try:
            from notifications.router import NotificationRouter, NotificationEvent
            if job.submitted_by:
                router = NotificationRouter(self.db)
                router.send(NotificationEvent(
                    event_type="job_started",
                    recipient_dev_id=job.submitted_by,
                    title=f"🚀 Job started: {job.title}",
                    body=(
                        f"Your factory job has left the queue and is now planning.\n\n"
                        f"Pipeline: planning → implementing → reviewing → qa → ready_for_approval"
                    ),
                    job_id=job.id,
                ))
        except Exception as e:
            logger.warning("job_started notification failed: %s", e)

        cli = self._get_cli("planning", job)
        project_root = self._get_project_root(job)

        # Inject lessons from past reviews
        lessons = get_review_lessons(job.project_id)
        lessons_section = ""
        if lessons:
            lessons_list = "\n".join(f"- {l}" for l in lessons)
            lessons_section = f"""
## Lessons from Past Reviews

The following issues have been flagged in previous factory reviews. Address these proactively in your plan:

{lessons_list}
"""

        prompt = f"""You are a software architect planning an implementation for an autonomous dev factory pipeline. Your plan will be handed to an implementation agent who will build it without human guidance, so be PRECISE and COMPLETE.

PROJECT: {job.project_slug}
FEATURE: {job.title}

SPEC:
{job.spec or job.description or job.title}

{DEVBRAIN_INSTRUCTIONS}
{lessons_section}

## Your Job

1. Search DevBrain for relevant past decisions, patterns, and issues FIRST
2. Read existing code to understand current architecture and patterns
3. Read the project's CLAUDE.md for conventions and constraints

Then create a detailed implementation plan covering:
- Exact files to create or modify (with full paths from project root)
- For each file: what to add/change, with enough detail that another agent can implement it without guessing
- Architecture approach and rationale (reference DevBrain decisions if relevant)
- Database migrations needed (if any)
- Test strategy: which test files to create, what to test
- HIPAA/compliance considerations (if applicable)
- Dependencies between tasks (what must be done in order)

Be specific about function names, class names, API endpoints, component names, and prop interfaces. The implementation agent will follow your plan literally.

Store the final plan in DevBrain using the store tool with type="decision"."""

        logger.info("Planning with %s...", cli)
        result = run_cli(cli, prompt, cwd=project_root,
                         env_override={"DEVBRAIN_PROJECT": job.project_slug})

        self.db.store_artifact(
            job_id=job.id,
            phase="planning",
            artifact_type="plan_doc",
            content=result.stdout,
            model_used=cli,
        )

        if result.success:
            # Extract files the plan will modify
            plan_files = extract_files_from_plan(result.stdout)

            # Attempt to acquire file locks
            registry = FileRegistry(self.db)
            lock_result = registry.acquire_locks(
                job_id=job.id,
                project_id=job.project_id,
                file_paths=plan_files,
                dev_id=job.submitted_by,
            )

            if not lock_result.success:
                # File conflicts — go to WAITING
                blocking_job_id = lock_result.conflicts[0]["blocking_job_id"] if lock_result.conflicts else None
                logger.info(
                    "Job %s has %d file conflicts — transitioning to WAITING",
                    job.id[:8], len(lock_result.conflicts),
                )
                notify_desktop(
                    "DevBrain Factory",
                    f"Job waiting on file locks: {job.title}",
                )
                self.db.store_artifact(
                    job_id=job.id,
                    phase="planning",
                    artifact_type="lock_conflicts",
                    content=json.dumps(lock_result.conflicts, indent=2),
                )
                # Set blocked_by_job_id via direct SQL (transition doesn't support this field)
                with self.db._conn() as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE devbrain.factory_jobs SET blocked_by_job_id = %s WHERE id = %s",
                        (blocking_job_id, job.id),
                    )
                    conn.commit()

                # Look up blocking dev for notification
                blocking_dev_id = None
                if blocking_job_id:
                    blocking_job = self.db.get_job(blocking_job_id)
                    if blocking_job:
                        blocking_dev_id = blocking_job.submitted_by

                # Fire lock_conflict notification to both devs
                try:
                    from notifications.router import NotificationRouter, NotificationEvent
                    router = NotificationRouter(self.db)
                    if job.submitted_by:
                        conflict_files = [c["file_path"] for c in lock_result.conflicts]
                        router.send_multi(NotificationEvent(
                            event_type="lock_conflict",
                            recipient_dev_id=job.submitted_by,
                            title=f"🔒 Job waiting on file locks: {job.title}",
                            body=(
                                "Your job is blocked by another dev's job.\n\n"
                                "Conflicting files:\n" +
                                "\n".join(f"  • {f}" for f in conflict_files) +
                                (f"\n\nBlocking dev: {blocking_dev_id}" if blocking_dev_id else "")
                            ),
                            job_id=job.id,
                            metadata={
                                "blocking_dev_id": blocking_dev_id,
                                "blocking_job_id": blocking_job_id,
                                "conflicts": lock_result.conflicts,
                            },
                        ))
                except Exception as e:
                    logger.warning("Lock conflict notification failed: %s", e)

                return self.db.transition(
                    job.id, JobStatus.WAITING,
                    metadata={"lock_conflicts": lock_result.conflicts},
                )

            # No conflicts — create branch and proceed
            slug = re.sub(r'[^a-z0-9-]', '-', job.title.lower())[:40].strip('-')
            branch = f"factory/{job.id[:8]}/{slug}"
            try:
                subprocess.run(
                    ["git", "checkout", "-b", branch],
                    cwd=project_root, capture_output=True, timeout=10,
                )
            except Exception as e:
                logger.warning("Branch creation failed: %s", e)
                branch = None

            return self.db.transition(job.id, JobStatus.IMPLEMENTING, branch_name=branch)
        else:
            return self.db.transition(job.id, JobStatus.FAILED,
                                      metadata={"failure": f"Planning failed: {result.stderr[:500]}"})

    # ─── Waiting Phase ─────────────────────────────────────────────────────

    def _run_waiting(self, job: FactoryJob) -> FactoryJob:
        """Waiting phase: job is blocked by file lock conflicts.

        Poll the registry to see if the blocking job has released its locks.
        Times out after 1 hour and fails the job.
        """
        import time

        registry = FileRegistry(self.db)

        # Get the files this job needs (from planning artifact)
        artifacts = self.db.get_artifacts(job.id, phase="planning")
        plan_artifact = next(
            (a for a in artifacts if a["artifact_type"] == "plan_doc"),
            None,
        )
        if not plan_artifact:
            logger.warning("WAITING job %s has no plan artifact — failing", job.id[:8])
            return self.db.transition(
                job.id, JobStatus.FAILED,
                metadata={"failure": "No plan artifact for WAITING job"},
            )

        plan_files = extract_files_from_plan(plan_artifact["content"])

        # Poll loop: check every 30 seconds, max 1 hour
        max_wait_seconds = 3600
        poll_interval = 30
        waited = 0

        while waited < max_wait_seconds:
            # Clean up expired locks (safety net for crashed jobs)
            registry.cleanup_expired_locks()

            # Try to acquire locks
            lock_result = registry.acquire_locks(
                job_id=job.id,
                project_id=job.project_id,
                file_paths=plan_files,
                dev_id=job.submitted_by,
            )

            if lock_result.success:
                # Clear blocked_by_job_id
                with self.db._conn() as conn, conn.cursor() as cur:
                    cur.execute(
                        "UPDATE devbrain.factory_jobs SET blocked_by_job_id = NULL WHERE id = %s",
                        (job.id,),
                    )
                    conn.commit()

                # Create branch and move to IMPLEMENTING
                project_root = self._get_project_root(job)
                slug = re.sub(r'[^a-z0-9-]', '-', job.title.lower())[:40].strip('-')
                branch = f"factory/{job.id[:8]}/{slug}"
                try:
                    subprocess.run(
                        ["git", "checkout", "-b", branch],
                        cwd=project_root, capture_output=True, timeout=10,
                    )
                except Exception as e:
                    logger.warning("Branch creation failed: %s", e)
                    branch = None

                logger.info("Job %s unblocked after %ds wait", job.id[:8], waited)
                notify_desktop(
                    "DevBrain Factory",
                    f"Job unblocked: {job.title}",
                )

                # Fire unblocked notification
                try:
                    from notifications.router import NotificationRouter, NotificationEvent
                    router = NotificationRouter(self.db)
                    if job.submitted_by:
                        router.send(NotificationEvent(
                            event_type="unblocked",
                            recipient_dev_id=job.submitted_by,
                            title=f"🔓 Job unblocked: {job.title}",
                            body="Your job is no longer blocked on file locks and is now implementing.",
                            job_id=job.id,
                        ))
                except Exception as e:
                    logger.warning("Unblock notification failed: %s", e)

                return self.db.transition(
                    job.id, JobStatus.IMPLEMENTING, branch_name=branch,
                )

            time.sleep(poll_interval)
            waited += poll_interval

        # Timeout — fail the job
        logger.warning("Job %s waited %ds without clearing — failing", job.id[:8], waited)
        return self.db.transition(
            job.id, JobStatus.FAILED,
            metadata={"failure": f"Waited {waited}s for file locks without clearing"},
        )

    # ─── Implementation Phase ──────────────────────────────────────────────

    def _run_implementation(self, job: FactoryJob) -> FactoryJob:
        """Implementation phase: write code and tests."""
        cli = self._get_cli("implementing", job)
        project_root = self._get_project_root(job)

        # Get the plan artifact
        plans = self.db.get_artifacts(job.id, phase="planning")
        plan_content = plans[-1]["content"] if plans else "No plan available"

        prompt = f"""You are implementing a feature based on an approved plan. You are part of an autonomous dev factory pipeline — there is no human to ask questions. Follow the plan precisely.

PROJECT: {job.project_slug}
FEATURE: {job.title}
BRANCH: {job.branch_name or 'main'}

IMPLEMENTATION PLAN:
{plan_content[:12000]}

{DEVBRAIN_INSTRUCTIONS}

## Before Implementing — Search DevBrain

Before writing any code, search DevBrain for:
1. Past patterns used in this project (type="pattern") — follow established patterns
2. Past issues/bugs found in similar features (type="issue") — avoid repeating mistakes
3. Relevant architecture decisions (type="decision") — respect prior decisions
Use what you find to avoid repeating past mistakes and follow established patterns.

## Your Job

1. Search DevBrain for relevant patterns and past decisions before coding
2. Read the project's CLAUDE.md for coding conventions and commit message format
3. Implement the code changes described in the plan — follow it precisely
4. Write tests for all new functionality
5. Run the project's lint and test commands to verify everything passes
6. Fix any lint or test failures before finishing
7. Commit your changes with conventional commit messages (follow CLAUDE.md format)
8. Do NOT push to remote — leave that for the approval step
9. Store any important decisions or patterns you discover in DevBrain

IMPORTANT: Follow existing code patterns in the repo. Read similar files before writing new ones. Match the project's style, naming conventions, and architecture patterns."""

        logger.info("Implementing with %s...", cli)
        result = run_cli(cli, prompt, cwd=project_root,
                         env_override={"DEVBRAIN_PROJECT": job.project_slug})

        self.db.store_artifact(
            job_id=job.id,
            phase="implementation",
            artifact_type="impl_output",
            content=result.stdout,
            model_used=cli,
        )

        # Capture the diff
        try:
            diff_result = subprocess.run(
                ["git", "diff", "main...HEAD", "--stat"],
                cwd=project_root, capture_output=True, text=True, timeout=10,
            )
            if diff_result.stdout:
                self.db.store_artifact(
                    job_id=job.id,
                    phase="implementation",
                    artifact_type="diff",
                    content=diff_result.stdout,
                )
        except Exception:
            pass

        if result.success:
            return self.db.transition(job.id, JobStatus.REVIEWING)
        else:
            return self.db.transition(job.id, JobStatus.FAILED,
                                      metadata={"failure": f"Implementation failed: {result.stderr[:500]}"})

    # ─── Review Phase ──────────────────────────────────────────────────────

    def _run_review(self, job: FactoryJob) -> FactoryJob:
        """Review phase: architecture + security/HIPAA review."""
        if job.status != JobStatus.REVIEWING:
            job = self.db.transition(job.id, JobStatus.REVIEWING)
        project_root = self._get_project_root(job)

        # Get the diff for review
        try:
            diff_result = subprocess.run(
                ["git", "diff", "main...HEAD"],
                cwd=project_root, capture_output=True, text=True, timeout=10,
            )
            diff_content = diff_result.stdout[:15000]
        except Exception:
            diff_content = "Unable to generate diff"

        # Build convergence context from prior rounds
        prior_arch = self._get_prior_findings(job, "arch_review")
        prior_sec = self._get_prior_findings(job, "security_review")
        fix_history = self._get_fix_history(job)

        convergence_note = ""
        if prior_arch or prior_sec or fix_history:
            convergence_note = f"""
## IMPORTANT: This is review round {job.error_count + 1}

Previous BLOCKING findings have been addressed by the fix agent. Your job is to verify fixes and identify ONLY genuinely new issues. Do NOT re-report findings that have been fixed.

{"### Previously reported architecture findings (verify these are fixed):" if prior_arch else ""}
{chr(10).join(f"- {f[:200]}" for f in prior_arch) if prior_arch else ""}

{"### Previously reported security findings (verify these are fixed):" if prior_sec else ""}
{chr(10).join(f"- {f[:200]}" for f in prior_sec) if prior_sec else ""}

{"### Fix history:" if fix_history else ""}
{fix_history[:3000] if fix_history else ""}

CONVERGENCE RULES:
1. If a previously reported finding is now fixed → mark it as RESOLVED, do not re-report as BLOCKING
2. Only report NEW BLOCKING findings that were NOT in previous rounds
3. If the same issue persists despite a fix attempt → report it as BLOCKING with note "persists from round N"
4. Do NOT expand scope — review only the diff, not the entire codebase
"""

        # Architecture review
        arch_cli = self._get_cli("review_arch", job)
        arch_prompt = f"""You are a senior software architect reviewing code changes for a dev factory pipeline.

PROJECT: {job.project_slug}
FEATURE: {job.title}
{convergence_note}

DIFF:
{diff_content}

{DEVBRAIN_INSTRUCTIONS}

## Before Reviewing — Search DevBrain

Before reviewing, search DevBrain for past review findings on similar features in this project.
If you find relevant past findings, check whether those same issues appear in this diff.

## Review Checklist

1. Code quality and maintainability
2. Architecture patterns and consistency with existing codebase
3. Test coverage (are all new code paths tested?)
4. Error handling completeness
5. Performance considerations

## Output Format

Output findings as a numbered list with severity:
- **BLOCKING**: Must fix before merge — only for genuine bugs, broken functionality, or missing critical behavior
- **WARNING**: Should fix but not blocking — code smell, missing edge case handling, suboptimal pattern
- **NIT**: Style/preference suggestions

Be precise: include file paths and line numbers. Only use BLOCKING for issues that would cause runtime errors, data corruption, or security vulnerabilities. Architectural preferences are WARNING, not BLOCKING.

If this is a re-review round, explicitly state which prior findings are RESOLVED vs still BLOCKING."""

        logger.info("Architecture review with %s...", arch_cli)
        arch_result = run_cli(arch_cli, arch_prompt, cwd=project_root,
                              env_override={"DEVBRAIN_PROJECT": job.project_slug})

        blocking_count = _count_blocking(arch_result.stdout)
        self.db.store_artifact(
            job_id=job.id,
            phase="review",
            artifact_type="arch_review",
            content=arch_result.stdout,
            model_used=arch_cli,
            findings_count=arch_result.stdout.count("\n- ") + arch_result.stdout.count("\n1."),
            blocking_count=blocking_count,
        )

        # Security/HIPAA review
        sec_cli = self._get_cli("review_security", job)
        sec_prompt = f"""You are a security auditor reviewing code changes for HIPAA compliance in a healthcare operations platform.

PROJECT: {job.project_slug}
FEATURE: {job.title}
{convergence_note}

DIFF:
{diff_content}

{DEVBRAIN_INSTRUCTIONS}

## Before Reviewing — Search DevBrain

Before reviewing, search DevBrain for past security findings and compliance issues in this project.
If you find relevant past findings, check whether those same issues appear in this diff.

## Review Checklist

1. PHI (Protected Health Information) exposure in logs, error messages, or API responses
2. Authentication and authorization gaps (missing scope checks, privilege escalation)
3. SQL injection, XSS, or other OWASP Top 10 vulnerabilities
4. Data validation at system boundaries (user input, external APIs)
5. FERPA compliance (student data protection)
6. Audit trail completeness for access control changes

## Output Format

Output findings as a numbered list with severity:
- **BLOCKING**: Security/compliance issue that MUST be fixed — actual vulnerability, PHI exposure, missing auth check
- **WARNING**: Potential concern that should be addressed — defense-in-depth suggestion, missing validation
- **NIT**: Best practice suggestion

Be precise: include file paths and line numbers. Only use BLOCKING for actual security vulnerabilities or compliance violations, not theoretical concerns or defense-in-depth suggestions (those are WARNING).

If this is a re-review round, explicitly state which prior findings are RESOLVED vs still BLOCKING.

Store any security issues found in DevBrain with type="issue" and category="security"."""

        logger.info("Security review with %s...", sec_cli)
        sec_result = run_cli(sec_cli, sec_prompt, cwd=project_root,
                             env_override={"DEVBRAIN_PROJECT": job.project_slug})

        sec_blocking = _count_blocking(sec_result.stdout)
        self.db.store_artifact(
            job_id=job.id,
            phase="review",
            artifact_type="security_review",
            content=sec_result.stdout,
            model_used=sec_cli,
            findings_count=sec_result.stdout.count("\n- ") + sec_result.stdout.count("\n1."),
            blocking_count=sec_blocking,
        )

        total_blocking = blocking_count + sec_blocking
        if total_blocking > 0:
            return self.db.transition(job.id, JobStatus.FIX_LOOP,
                                      metadata={"blocking_findings": total_blocking})
        else:
            return self.db.transition(job.id, JobStatus.QA)

    # ─── QA Phase ──────────────────────────────────────────────────────────

    def _run_qa(self, job: FactoryJob) -> FactoryJob:
        """QA phase: run full test suite, lint, type checks."""
        if job.status != JobStatus.QA:
            job = self.db.transition(job.id, JobStatus.QA)
        project_root = self._get_project_root(job)

        # Get project test/lint commands from DB
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT metadata FROM devbrain.projects WHERE id = %s",
                (job.project_id,),
            )
            row = cur.fetchone()
            project_meta = row[0] if row and row[0] else {}

        lint_cmds = project_meta.get("lint_commands", {})
        test_cmds = project_meta.get("test_commands", {})

        results = []
        all_passed = True

        # Run lint commands
        for name, cmd in (lint_cmds or {}).items():
            logger.info("QA lint: %s", name)
            try:
                r = subprocess.run(
                    cmd, shell=True, cwd=project_root,
                    capture_output=True, text=True, timeout=120,
                )
                passed = r.returncode == 0
                results.append({"check": f"lint:{name}", "passed": passed, "output": r.stdout[-500:]})
                if not passed:
                    all_passed = False
            except Exception as e:
                results.append({"check": f"lint:{name}", "passed": False, "output": str(e)})
                all_passed = False

        # Run test commands
        for name, cmd in (test_cmds or {}).items():
            logger.info("QA test: %s", name)
            try:
                r = subprocess.run(
                    cmd, shell=True, cwd=project_root,
                    capture_output=True, text=True, timeout=300,
                )
                passed = r.returncode == 0
                results.append({"check": f"test:{name}", "passed": passed, "output": r.stdout[-500:]})
                if not passed:
                    all_passed = False
            except Exception as e:
                results.append({"check": f"test:{name}", "passed": False, "output": str(e)})
                all_passed = False

        self.db.store_artifact(
            job_id=job.id,
            phase="qa",
            artifact_type="qa_report",
            content=json.dumps(results, indent=2),
            findings_count=len(results),
            blocking_count=sum(1 for r in results if not r["passed"]),
        )

        if all_passed:
            return self.db.transition(job.id, JobStatus.READY_FOR_APPROVAL)
        else:
            return self.db.transition(job.id, JobStatus.FIX_LOOP,
                                      metadata={"qa_failures": [r["check"] for r in results if not r["passed"]]})

    # ─── Fix Loop ──────────────────────────────────────────────────────────

    def _run_fix(self, job: FactoryJob) -> FactoryJob:
        """Fix loop: address blocking findings from the most recent review round."""
        cli = self._get_cli("fix", job)
        project_root = self._get_project_root(job)

        # Get ONLY the most recent review artifacts (not all historical ones)
        all_artifacts = self.db.get_artifacts(job.id)
        latest_blocking = []
        for art in reversed(all_artifacts):
            if art["phase"] == "review" and art["blocking_count"] > 0:
                items = _extract_blocking_items(art["content"])
                latest_blocking.extend(items)
            # Stop once we've passed the most recent review round
            if art["phase"] == "fix":
                break

        if not latest_blocking:
            # QA failures instead of review findings
            qa_artifacts = [a for a in all_artifacts if a["phase"] == "qa" and a["blocking_count"] > 0]
            if qa_artifacts:
                latest_blocking.append(qa_artifacts[-1]["content"])

        fix_prompt = f"""You are fixing blocking issues found during code review. You are part of an autonomous dev factory pipeline — fix ONLY what is listed below, nothing else.

PROJECT: {job.project_slug}
FEATURE: {job.title}
BRANCH: {job.branch_name or 'main'}
FIX ATTEMPT: {job.error_count + 1}/{job.max_retries}

## BLOCKING FINDINGS TO FIX

{chr(10).join(f"{i+1}. {finding}" for i, finding in enumerate(latest_blocking))}

{DEVBRAIN_INSTRUCTIONS}

## Your Job

1. Read each blocking finding carefully — understand the specific file and line
2. Read the actual code at those locations
3. Apply the MINIMUM fix needed to resolve each finding
4. Run lint and tests to verify your fixes don't break anything
5. Commit the fixes with conventional commit messages
6. Do NOT push to remote
7. Do NOT refactor or improve code beyond what the findings require
8. Do NOT add new features or change behavior beyond the fix

IMPORTANT: Fix ONLY the listed findings. Do not expand scope. Do not "improve" surrounding code. Minimal, targeted fixes only."""

        logger.info("Fix loop attempt %d with %s...", job.error_count + 1, cli)
        result = run_cli(cli, fix_prompt, cwd=project_root,
                         env_override={"DEVBRAIN_PROJECT": job.project_slug})

        self.db.store_artifact(
            job_id=job.id,
            phase="fix",
            artifact_type="fix_output",
            content=result.stdout,
            model_used=cli,
        )

        # Transition back to implementing (which will re-trigger review)
        return self.db.transition(job.id, JobStatus.IMPLEMENTING)

    # ─── Approval ──────────────────────────────────────────────────────────

    def approve_job(self, job_id: str, notes: str | None = None) -> FactoryJob:
        """Approve a job — pushes the branch."""
        job = self.db.get_job(job_id)
        if not job or job.status != JobStatus.READY_FOR_APPROVAL:
            raise ValueError(f"Job {job_id} is not ready for approval (status: {job.status.value if job else 'not found'})")

        project_root = self._get_project_root(job)

        # Push the branch
        if job.branch_name:
            try:
                subprocess.run(
                    ["git", "push", "-u", "origin", job.branch_name],
                    cwd=project_root, capture_output=True, timeout=30,
                )
            except Exception as e:
                logger.warning("Push failed: %s", e)

        job = self.db.transition(job.id, JobStatus.APPROVED,
                                 metadata={"approved_at": "now", "notes": notes or ""})
        notify_desktop("DevBrain Factory", f"Approved: {job.title}")
        return job

    def reject_job(self, job_id: str, notes: str | None = None) -> FactoryJob:
        """Reject a job."""
        return self.db.transition(job_id, JobStatus.REJECTED,
                                  metadata={"rejected_reason": notes or ""})
