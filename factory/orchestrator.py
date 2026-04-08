"""Dev Factory orchestrator.

Runs the autonomous development pipeline:
queued → planning → implementing → reviewing → qa → ready_for_approval

Each phase spawns a CLI tool (claude, codex, gemini) as a subprocess.
Human approval is required before push/deploy.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from state_machine import FactoryDB, FactoryJob, JobStatus
from cli_executor import run_cli, notify_desktop, DEFAULT_CLI_ASSIGNMENTS

logger = logging.getLogger(__name__)


class FactoryOrchestrator:
    """Orchestrates the dev factory pipeline."""

    def __init__(self, database_url: str):
        self.db = FactoryDB(database_url)

    def run_job(self, job_id: str) -> FactoryJob:
        """Run a job through the full pipeline until it needs human approval or fails."""
        job = self.db.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

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
            elif job.status == JobStatus.PLANNING:
                job = self._run_implementation(job)
            elif job.status == JobStatus.IMPLEMENTING:
                job = self._run_review(job)
            elif job.status == JobStatus.REVIEWING:
                job = self._run_qa(job)
            elif job.status == JobStatus.QA:
                # QA passed — ready for human approval
                break
            elif job.status == JobStatus.FIX_LOOP:
                if job.error_count >= job.max_retries:
                    job = self.db.transition(job.id, JobStatus.FAILED,
                                             metadata={"failure": "max fix retries exceeded"})
                    notify_desktop("DevBrain Factory", f"Job FAILED: {job.title} (max retries)")
                    break
                job = self._run_fix(job)

        if job.status == JobStatus.READY_FOR_APPROVAL:
            notify_desktop("DevBrain Factory",
                           f"Ready for review: {job.title}")

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

    def _run_planning(self, job: FactoryJob) -> FactoryJob:
        """Planning phase: generate implementation plan from spec."""
        job = self.db.transition(job.id, JobStatus.PLANNING)
        cli = self._get_cli("planning", job)
        project_root = self._get_project_root(job)

        prompt = f"""You are a software architect planning an implementation.

PROJECT: {job.project_slug}
FEATURE: {job.title}
SPEC: {job.spec or job.description or job.title}

Search DevBrain for relevant context (past decisions, patterns, issues) before planning.

Create a detailed implementation plan covering:
1. Files to create or modify (with specific paths)
2. Architecture approach and rationale
3. Test strategy (what tests to write)
4. HIPAA/compliance considerations (if applicable)
5. Estimated number of files changed

Output the plan as a structured document. Be specific about file paths and function names.
Store the plan in DevBrain using the store tool with type="decision"."""

        logger.info("Planning with %s...", cli)
        result = run_cli(cli, prompt, cwd=project_root)

        self.db.store_artifact(
            job_id=job.id,
            phase="planning",
            artifact_type="plan_doc",
            content=result.stdout,
            model_used=cli,
        )

        if result.success:
            # Create branch for this job
            slug = job.title.lower().replace(" ", "-")[:40]
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

    def _run_implementation(self, job: FactoryJob) -> FactoryJob:
        """Implementation phase: write code and tests."""
        cli = self._get_cli("implementing", job)
        project_root = self._get_project_root(job)

        # Get the plan artifact
        plans = self.db.get_artifacts(job.id, phase="planning")
        plan_content = plans[-1]["content"] if plans else "No plan available"

        prompt = f"""You are implementing a feature based on an approved plan.

PROJECT: {job.project_slug}
FEATURE: {job.title}
BRANCH: {job.branch_name or 'main'}

IMPLEMENTATION PLAN:
{plan_content[:8000]}

Instructions:
1. Implement the code changes described in the plan
2. Write tests for all new functionality
3. Run the project's lint and test commands to verify
4. Fix any lint or test failures before finishing
5. Commit your changes with conventional commit messages
6. Do NOT push to remote — leave that for approval

Store any important decisions or patterns you discover in DevBrain."""

        logger.info("Implementing with %s...", cli)
        result = run_cli(cli, prompt, cwd=project_root)

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

    def _run_review(self, job: FactoryJob) -> FactoryJob:
        """Review phase: architecture + security/HIPAA review."""
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

        # Architecture review
        arch_cli = self._get_cli("review_arch", job)
        arch_prompt = f"""You are a senior software architect reviewing code changes.

PROJECT: {job.project_slug}
FEATURE: {job.title}

DIFF:
{diff_content}

Review for:
1. Code quality and maintainability
2. Architecture patterns and consistency
3. Test coverage (are all new paths tested?)
4. Error handling completeness
5. Performance considerations

Output findings as a list with severity levels:
- BLOCKING: Must fix before merge
- WARNING: Should fix but not blocking
- NIT: Style/preference suggestions

Store any important patterns or issues found in DevBrain."""

        logger.info("Architecture review with %s...", arch_cli)
        arch_result = run_cli(arch_cli, arch_prompt, cwd=project_root)

        blocking_count = arch_result.stdout.lower().count("blocking")
        self.db.store_artifact(
            job_id=job.id,
            phase="review",
            artifact_type="arch_review",
            content=arch_result.stdout,
            model_used=arch_cli,
            findings_count=arch_result.stdout.count("\n- "),
            blocking_count=blocking_count,
        )

        # Security/HIPAA review
        sec_cli = self._get_cli("review_security", job)
        sec_prompt = f"""You are a security auditor reviewing code changes for HIPAA compliance.

PROJECT: {job.project_slug}
FEATURE: {job.title}

DIFF:
{diff_content}

Review for:
1. PHI (Protected Health Information) exposure in logs, errors, or responses
2. Authentication and authorization gaps
3. SQL injection, XSS, or other OWASP Top 10 vulnerabilities
4. Data validation at system boundaries
5. FERPA compliance (student data protection)

Output findings with severity:
- BLOCKING: Security/compliance issue, must fix
- WARNING: Potential concern, should address
- NIT: Best practice suggestion

Store any security issues found in DevBrain with type="issue" and category="security"."""

        logger.info("Security review with %s...", sec_cli)
        sec_result = run_cli(sec_cli, sec_prompt, cwd=project_root)

        sec_blocking = sec_result.stdout.lower().count("blocking")
        self.db.store_artifact(
            job_id=job.id,
            phase="review",
            artifact_type="security_review",
            content=sec_result.stdout,
            model_used=sec_cli,
            findings_count=sec_result.stdout.count("\n- "),
            blocking_count=sec_blocking,
        )

        total_blocking = blocking_count + sec_blocking
        if total_blocking > 0:
            return self.db.transition(job.id, JobStatus.FIX_LOOP,
                                      metadata={"blocking_findings": total_blocking})
        else:
            return self.db.transition(job.id, JobStatus.QA)

    def _run_qa(self, job: FactoryJob) -> FactoryJob:
        """QA phase: run full test suite, lint, type checks."""
        job = self.db.transition(job.id, JobStatus.QA)
        project_root = self._get_project_root(job)

        # Get project test/lint commands
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT lint_commands, test_commands FROM devbrain.projects WHERE id = %s",
                (job.project_id,),
            )
            row = cur.fetchone()
            lint_cmds = row[0] if row else {}
            test_cmds = row[1] if row else {}

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

    def _run_fix(self, job: FactoryJob) -> FactoryJob:
        """Fix loop: address blocking findings from review or QA failures."""
        cli = self._get_cli("fix", job)
        project_root = self._get_project_root(job)

        # Gather all blocking findings
        artifacts = self.db.get_artifacts(job.id)
        blocking_findings = []
        for art in artifacts:
            if art["blocking_count"] > 0:
                blocking_findings.append(f"[{art['artifact_type']}]\n{art['content']}")

        fix_prompt = f"""You need to fix blocking issues found during review/QA.

PROJECT: {job.project_slug}
FEATURE: {job.title}
BRANCH: {job.branch_name or 'main'}
ATTEMPT: {job.error_count + 1}/{job.max_retries}

BLOCKING FINDINGS:
{chr(10).join(blocking_findings)[-8000:]}

Instructions:
1. Read each blocking finding carefully
2. Apply the minimum fix needed to resolve each issue
3. Run lint and tests to verify fixes
4. Commit the fixes with conventional commit messages
5. Do NOT push to remote"""

        logger.info("Fix loop attempt %d with %s...", job.error_count + 1, cli)
        result = run_cli(cli, fix_prompt, cwd=project_root)

        self.db.store_artifact(
            job_id=job.id,
            phase="fix",
            artifact_type="fix_output",
            content=result.stdout,
            model_used=cli,
        )

        # Transition back to implementing (which will re-trigger review)
        return self.db.transition(job.id, JobStatus.IMPLEMENTING)

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
