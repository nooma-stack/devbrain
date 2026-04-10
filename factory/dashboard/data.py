"""Dashboard data queries — pulls factory state from the DevBrain DB."""

from __future__ import annotations

from state_machine import FactoryDB


class DashboardData:
    """Read-only data access for the dashboard.

    All queries are snapshot reads against the DevBrain DB. The dashboard
    polls this class on a tick to refresh its views.
    """

    def __init__(self, db: FactoryDB):
        self.db = db

    def get_active_jobs(self, project: str | None = None, limit: int = 20) -> list[dict]:
        """Jobs in flight: not terminal, not archived."""
        conditions = [
            "j.status NOT IN ('approved', 'rejected', 'deployed', 'failed')",
            "j.archived_at IS NULL",
        ]
        params: list = []
        if project:
            conditions.append("p.slug = %s")
            params.append(project)

        where = " AND ".join(conditions)
        params.append(limit)

        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT j.id, j.title, j.status, j.current_phase, j.submitted_by,
                       j.error_count, j.max_retries, j.branch_name,
                       j.updated_at, j.blocked_by_job_id, p.slug
                FROM devbrain.factory_jobs j
                JOIN devbrain.projects p ON j.project_id = p.id
                WHERE {where}
                ORDER BY j.updated_at DESC
                LIMIT %s
                """,
                params,
            )
            return [
                {
                    "id": str(r[0]),
                    "title": r[1],
                    "status": r[2],
                    "current_phase": r[3],
                    "submitted_by": r[4],
                    "error_count": r[5],
                    "max_retries": r[6],
                    "branch_name": r[7],
                    "updated_at": r[8],
                    "blocked_by_job_id": str(r[9]) if r[9] else None,
                    "project": r[10],
                }
                for r in cur.fetchall()
            ]

    def get_recent_events(
        self,
        project: str | None = None,
        limit: int = 30,
        since_minutes: int = 60,
    ) -> list[dict]:
        """Recent factory activity — artifact creations + cleanup reports."""
        conditions = [f"a.created_at > now() - interval '{int(since_minutes)} minutes'"]
        params: list = []
        if project:
            conditions.append("p.slug = %s")
            params.append(project)

        where = " AND ".join(conditions)
        params.append(limit)

        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT a.job_id, j.title, a.phase, a.artifact_type,
                       a.findings_count, a.blocking_count,
                       a.created_at, p.slug, j.status
                FROM devbrain.factory_artifacts a
                JOIN devbrain.factory_jobs j ON a.job_id = j.id
                JOIN devbrain.projects p ON j.project_id = p.id
                WHERE {where}
                ORDER BY a.created_at DESC
                LIMIT %s
                """,
                params,
            )
            rows = cur.fetchall()

        events = []
        for r in rows:
            blocking = r[5] or 0
            findings = r[4] or 0
            if r[3] in ("arch_review", "security_review"):
                summary = f"{r[3]}: {blocking} blocking, {findings - blocking} other findings"
            elif r[3] == "plan_doc":
                summary = "planning complete"
            elif r[3] == "impl_output":
                summary = "implementation complete"
            elif r[3] == "qa_report":
                summary = "QA complete"
            elif r[3] == "diff":
                summary = "diff captured"
            elif r[3] == "lock_conflicts":
                summary = "BLOCKED on file lock conflicts"
            else:
                summary = r[3]

            events.append({
                "job_id": str(r[0]),
                "job_title": r[1],
                "phase": r[2],
                "artifact_type": r[3],
                "summary": summary,
                "blocking_count": blocking,
                "timestamp": r[6],
                "project": r[7],
                "job_status": r[8],
            })
        return events

    def get_active_locks(self, project: str | None = None) -> list[dict]:
        """Currently held file locks."""
        conditions = ["fl.expires_at > now()"]
        params: list = []
        if project:
            conditions.append("p.slug = %s")
            params.append(project)

        where = " AND ".join(conditions)

        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT fl.file_path, fl.dev_id, fl.locked_at, fl.expires_at,
                       j.id, j.title, j.status, p.slug
                FROM devbrain.file_locks fl
                JOIN devbrain.factory_jobs j ON fl.job_id = j.id
                JOIN devbrain.projects p ON j.project_id = p.id
                WHERE {where}
                ORDER BY fl.locked_at ASC
                """,
                params,
            )
            return [
                {
                    "file_path": r[0],
                    "dev_id": r[1],
                    "locked_at": r[2],
                    "expires_at": r[3],
                    "job_id": str(r[4]),
                    "job_title": r[5],
                    "job_status": r[6],
                    "project": r[7],
                }
                for r in cur.fetchall()
            ]

    def get_recent_completed(
        self,
        project: str | None = None,
        hours: int = 24,
        limit: int = 15,
    ) -> list[dict]:
        """Jobs that reached a terminal state in the last N hours."""
        conditions = [
            "j.status IN ('approved', 'rejected', 'deployed', 'failed')",
            f"j.updated_at > now() - interval '{int(hours)} hours'",
        ]
        params: list = []
        if project:
            conditions.append("p.slug = %s")
            params.append(project)

        where = " AND ".join(conditions)
        params.append(limit)

        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT j.id, j.title, j.status, j.submitted_by,
                       j.updated_at, j.error_count, p.slug
                FROM devbrain.factory_jobs j
                JOIN devbrain.projects p ON j.project_id = p.id
                WHERE {where}
                ORDER BY j.updated_at DESC
                LIMIT %s
                """,
                params,
            )
            return [
                {
                    "id": str(r[0]),
                    "title": r[1],
                    "status": r[2],
                    "submitted_by": r[3],
                    "updated_at": r[4],
                    "error_count": r[5],
                    "project": r[6],
                }
                for r in cur.fetchall()
            ]

    def get_job_details(self, job_id: str) -> dict | None:
        """Full details for a single job — used by the detail modal."""
        job = self.db.get_job(job_id)
        if not job:
            return None

        artifacts = self.db.get_artifacts(job_id)
        reports = self.db.get_cleanup_reports(job_id)

        return {
            "id": job.id,
            "title": job.title,
            "status": job.status.value,
            "current_phase": job.current_phase,
            "submitted_by": job.submitted_by,
            "branch_name": job.branch_name,
            "error_count": job.error_count,
            "max_retries": job.max_retries,
            "spec": job.spec or "",
            "created_at": str(job.created_at),
            "updated_at": str(job.updated_at),
            "blocked_by_job_id": job.blocked_by_job_id,
            "blocked_resolution": job.blocked_resolution,
            "metadata": job.metadata,
            "artifacts": [
                {
                    "phase": a["phase"],
                    "artifact_type": a["artifact_type"],
                    "findings_count": a["findings_count"],
                    "blocking_count": a["blocking_count"],
                    "created_at": a["created_at"],
                    "content_preview": (a["content"] or "")[:500],
                }
                for a in artifacts
            ],
            "cleanup_reports": [
                {
                    "report_type": r["report_type"],
                    "outcome": r["outcome"],
                    "summary": r["summary"][:500],
                    "created_at": r["created_at"],
                }
                for r in reports
            ],
        }
