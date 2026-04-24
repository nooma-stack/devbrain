"""Dev Factory pipeline state machine.

Manages job lifecycle: queued → planning → implementing → reviewing → qa →
ready_for_approval → approved → deployed

Error states: fix_loop → implementing (retry), rejected → closed
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

import psycopg2
import psycopg2.extras

psycopg2.extras.register_uuid()

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    QUEUED = "queued"
    PLANNING = "planning"
    BLOCKED = "blocked"            # Was WAITING — conflicted on file locks, awaiting dev resolution
    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"
    QA = "qa"
    FIX_LOOP = "fix_loop"
    READY_FOR_APPROVAL = "ready_for_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    DEPLOYED = "deployed"
    FAILED = "failed"


# Valid state transitions
TRANSITIONS: dict[JobStatus, list[JobStatus]] = {
    JobStatus.QUEUED: [JobStatus.PLANNING],
    JobStatus.PLANNING: [JobStatus.IMPLEMENTING, JobStatus.BLOCKED, JobStatus.FAILED],
    JobStatus.BLOCKED: [
        JobStatus.IMPLEMENTING,  # proceed resolution
        JobStatus.PLANNING,      # replan resolution
        JobStatus.REJECTED,      # cancel resolution
        JobStatus.FAILED,        # safety net
    ],
    JobStatus.IMPLEMENTING: [JobStatus.REVIEWING, JobStatus.FAILED],
    JobStatus.REVIEWING: [JobStatus.QA, JobStatus.FIX_LOOP, JobStatus.FAILED],
    JobStatus.QA: [JobStatus.READY_FOR_APPROVAL, JobStatus.FIX_LOOP, JobStatus.FAILED],
    JobStatus.FIX_LOOP: [JobStatus.IMPLEMENTING, JobStatus.FAILED],
    JobStatus.READY_FOR_APPROVAL: [JobStatus.APPROVED, JobStatus.REJECTED],
    JobStatus.APPROVED: [JobStatus.DEPLOYED],
    JobStatus.REJECTED: [],
    JobStatus.DEPLOYED: [],
    JobStatus.FAILED: [JobStatus.QUEUED],  # Can re-queue failed jobs
}


@dataclass
class FactoryJob:
    id: str
    project_id: str
    project_slug: str
    title: str
    description: str | None
    spec: str | None
    status: JobStatus
    priority: int
    branch_name: str | None
    current_phase: str | None
    error_count: int
    max_retries: int
    assigned_cli: str | None
    metadata: dict
    created_at: datetime
    updated_at: datetime
    submitted_by: str | None = None
    blocked_by_job_id: str | None = None
    blocked_resolution: str | None = None


class FactoryDB:
    """Database operations for the factory pipeline."""

    TERMINAL_STATES = {
        JobStatus.APPROVED,
        JobStatus.REJECTED,
        JobStatus.DEPLOYED,
        JobStatus.FAILED,
    }

    def __init__(self, database_url: str):
        self.database_url = database_url

    def _conn(self):
        return psycopg2.connect(self.database_url)

    def create_job(
        self,
        project_slug: str,
        title: str,
        spec: str,
        description: str | None = None,
        priority: int = 0,
        assigned_cli: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        """Create a new factory job. Returns job ID."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM devbrain.projects WHERE slug = %s", (project_slug,)
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Project '{project_slug}' not found")
            project_id = row[0]

            cur.execute(
                """
                INSERT INTO devbrain.factory_jobs
                    (project_id, title, description, spec, status, priority,
                     current_phase, assigned_cli, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    project_id, title, description, spec,
                    JobStatus.QUEUED.value, priority,
                    "queued", assigned_cli,
                    json.dumps(metadata or {}),
                ),
            )
            job_id = str(cur.fetchone()[0])
            conn.commit()
            logger.info("Created factory job %s: %s", job_id[:8], title)
            return job_id

    def get_job(self, job_id: str) -> FactoryJob | None:
        """Get a job by ID."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT j.id, j.project_id, p.slug, j.title, j.description, j.spec,
                       j.status, j.priority, j.branch_name, j.current_phase,
                       j.error_count, j.max_retries, j.assigned_cli, j.metadata,
                       j.created_at, j.updated_at,
                       j.submitted_by, j.blocked_by_job_id, j.blocked_resolution
                FROM devbrain.factory_jobs j
                JOIN devbrain.projects p ON j.project_id = p.id
                WHERE j.id = %s
                """,
                (job_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return FactoryJob(
                id=str(row[0]), project_id=str(row[1]), project_slug=row[2],
                title=row[3], description=row[4], spec=row[5],
                status=JobStatus(row[6]), priority=row[7],
                branch_name=row[8], current_phase=row[9],
                error_count=row[10], max_retries=row[11],
                assigned_cli=row[12], metadata=row[13] or {},
                created_at=row[14], updated_at=row[15],
                submitted_by=row[16],
                blocked_by_job_id=str(row[17]) if row[17] else None,
                blocked_resolution=row[18],
            )

    def list_jobs(
        self,
        project_slug: str | None = None,
        status: JobStatus | None = None,
        active_only: bool = False,
    ) -> list[FactoryJob]:
        """List factory jobs with optional filters."""
        conditions = []
        params: list = []

        if project_slug:
            conditions.append("p.slug = %s")
            params.append(project_slug)
        if status:
            conditions.append("j.status = %s")
            params.append(status.value)
        if active_only:
            inactive = [JobStatus.APPROVED.value, JobStatus.REJECTED.value,
                        JobStatus.DEPLOYED.value, JobStatus.FAILED.value]
            conditions.append("j.status NOT IN %s")
            params.append(tuple(inactive))
            conditions.append("j.archived_at IS NULL")

        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT j.id, j.project_id, p.slug, j.title, j.description, j.spec,
                       j.status, j.priority, j.branch_name, j.current_phase,
                       j.error_count, j.max_retries, j.assigned_cli, j.metadata,
                       j.created_at, j.updated_at,
                       j.submitted_by, j.blocked_by_job_id, j.blocked_resolution
                FROM devbrain.factory_jobs j
                JOIN devbrain.projects p ON j.project_id = p.id
                {where}
                ORDER BY j.priority DESC, j.created_at ASC
                """,
                params,
            )
            return [
                FactoryJob(
                    id=str(r[0]), project_id=str(r[1]), project_slug=r[2],
                    title=r[3], description=r[4], spec=r[5],
                    status=JobStatus(r[6]), priority=r[7],
                    branch_name=r[8], current_phase=r[9],
                    error_count=r[10], max_retries=r[11],
                    assigned_cli=r[12], metadata=r[13] or {},
                    created_at=r[14], updated_at=r[15],
                    submitted_by=r[16],
                    blocked_by_job_id=str(r[17]) if r[17] else None,
                    blocked_resolution=r[18],
                )
                for r in cur.fetchall()
            ]

    def transition(self, job_id: str, new_status: JobStatus, **updates) -> FactoryJob:
        """Transition a job to a new status. Validates the transition is legal."""
        job = self.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        if new_status not in TRANSITIONS.get(job.status, []):
            raise ValueError(
                f"Invalid transition: {job.status.value} → {new_status.value}. "
                f"Valid: {[s.value for s in TRANSITIONS[job.status]]}"
            )

        set_clauses = ["status = %s", "current_phase = %s", "updated_at = now()"]
        params: list = [new_status.value, new_status.value]

        if new_status == JobStatus.FIX_LOOP:
            set_clauses.append("error_count = error_count + 1")

        for key, value in updates.items():
            if key == "branch_name":
                set_clauses.append("branch_name = %s")
                params.append(value)
            elif key == "assigned_cli":
                set_clauses.append("assigned_cli = %s")
                params.append(value)
            elif key == "metadata":
                set_clauses.append("metadata = metadata || %s::jsonb")
                params.append(json.dumps(value))

        params.append(job_id)

        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE devbrain.factory_jobs SET {', '.join(set_clauses)} WHERE id = %s",
                params,
            )
            conn.commit()

        logger.info("Job %s: %s → %s", job_id[:8], job.status.value, new_status.value)
        return self.get_job(job_id)

    def update_metadata(self, job_id: str, metadata: dict) -> None:
        """Merge the given dict into a job's metadata JSONB without
        changing status. Used when we need to attach diagnostic info
        (e.g., approve_sync_error) but the state must not advance."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE devbrain.factory_jobs "
                "SET metadata = metadata || %s::jsonb, updated_at = now() "
                "WHERE id = %s",
                (json.dumps(metadata), job_id),
            )
            conn.commit()

    def set_blocked_resolution(self, job_id: str, resolution: str) -> None:
        """Set the dev's resolution for a blocked job.

        resolution: 'proceed' | 'replan' | 'cancel'
        """
        if resolution not in ("proceed", "replan", "cancel"):
            raise ValueError(f"Invalid resolution: {resolution}")
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE devbrain.factory_jobs SET blocked_resolution = %s, updated_at = now() WHERE id = %s",
                (resolution, job_id),
            )
            conn.commit()

    def clear_blocked_resolution(self, job_id: str) -> None:
        """Clear the blocked_resolution field (called after factory consumes it)."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE devbrain.factory_jobs SET blocked_resolution = NULL, updated_at = now() WHERE id = %s",
                (job_id,),
            )
            conn.commit()

    def store_artifact(
        self,
        job_id: str,
        phase: str,
        artifact_type: str,
        content: str,
        model_used: str | None = None,
        findings_count: int = 0,
        blocking_count: int = 0,
        warning_count: int = 0,
        metadata: dict | None = None,
    ) -> str:
        """Store a factory artifact (plan, diff, review, QA report)."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO devbrain.factory_artifacts
                    (job_id, phase, artifact_type, content, model_used,
                     findings_count, blocking_count, warning_count, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    job_id, phase, artifact_type, content, model_used,
                    findings_count, blocking_count, warning_count,
                    json.dumps(metadata or {}),
                ),
            )
            artifact_id = str(cur.fetchone()[0])
            conn.commit()
            return artifact_id

    def get_artifacts(self, job_id: str, phase: str | None = None) -> list[dict]:
        """Get artifacts for a job, optionally filtered by phase."""
        conditions = ["job_id = %s"]
        params: list = [job_id]
        if phase:
            conditions.append("phase = %s")
            params.append(phase)

        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, phase, artifact_type, content, model_used,
                       findings_count, blocking_count, warning_count,
                       metadata, created_at
                FROM devbrain.factory_artifacts
                WHERE {' AND '.join(conditions)}
                ORDER BY created_at ASC
                """,
                params,
            )
            return [
                {
                    "id": str(r[0]), "phase": r[1], "artifact_type": r[2],
                    "content": r[3], "model_used": r[4],
                    "findings_count": r[5], "blocking_count": r[6],
                    "warning_count": r[7],
                    "metadata": r[8] or {}, "created_at": str(r[9]),
                }
                for r in cur.fetchall()
            ]

    def archive_job(self, job_id: str) -> None:
        """Mark a terminal job as archived by setting archived_at."""
        job = self.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        if job.status not in self.TERMINAL_STATES:
            allowed = sorted(s.value for s in self.TERMINAL_STATES)
            raise ValueError(
                f"Cannot archive job in status '{job.status.value}'. "
                f"Must be one of: {allowed}"
            )

        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE devbrain.factory_jobs SET archived_at = now() WHERE id = %s",
                (job_id,),
            )
            conn.commit()

        logger.info("Archived job %s", job_id[:8])

    def store_cleanup_report(
        self,
        job_id: str,
        report_type: str,
        outcome: str,
        summary: str,
        phases_traversed: list | None = None,
        artifacts_summary: dict | None = None,
        recovery_diagnosis: str | None = None,
        recovery_action_taken: str | None = None,
        time_elapsed_seconds: int | None = None,
        metadata: dict | None = None,
    ) -> str:
        """Insert a cleanup report and return its ID."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO devbrain.factory_cleanup_reports
                    (job_id, report_type, outcome, summary, phases_traversed,
                     artifacts_summary, recovery_diagnosis, recovery_action_taken,
                     time_elapsed_seconds, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    job_id,
                    report_type,
                    outcome,
                    summary,
                    json.dumps(phases_traversed or []),
                    json.dumps(artifacts_summary or {}),
                    recovery_diagnosis,
                    recovery_action_taken,
                    time_elapsed_seconds,
                    json.dumps(metadata or {}),
                ),
            )
            report_id = str(cur.fetchone()[0])
            conn.commit()
            return report_id

    def get_cleanup_reports(self, job_id: str) -> list[dict]:
        """Get all cleanup reports for a job, ordered by creation time."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, job_id, report_type, outcome, summary,
                       phases_traversed, artifacts_summary, recovery_diagnosis,
                       recovery_action_taken, time_elapsed_seconds, metadata,
                       created_at
                FROM devbrain.factory_cleanup_reports
                WHERE job_id = %s
                ORDER BY created_at ASC
                """,
                (job_id,),
            )
            return [
                {
                    "id": str(r[0]),
                    "job_id": str(r[1]),
                    "report_type": r[2],
                    "outcome": r[3],
                    "summary": r[4],
                    "phases_traversed": r[5] or [],
                    "artifacts_summary": r[6] or {},
                    "recovery_diagnosis": r[7],
                    "recovery_action_taken": r[8],
                    "time_elapsed_seconds": r[9],
                    "metadata": r[10] or {},
                    "created_at": str(r[11]),
                }
                for r in cur.fetchall()
            ]

    def get_next_queued(self, project_slug: str | None = None) -> FactoryJob | None:
        """Get the next queued job (highest priority, oldest first)."""
        jobs = self.list_jobs(project_slug=project_slug, status=JobStatus.QUEUED)
        return jobs[0] if jobs else None

    # ─── Devs + Notifications CRUD ───────────────────────────────────────────

    DEFAULT_EVENT_SUBSCRIPTIONS = [
        "job_started",
        "job_ready",
        "job_failed",
        "blocked",                # NEW
        "lock_conflict",
        "unblocked",
        "needs_human",
        "recovery_started",
        "recovery_succeeded",
    ]

    def register_dev(
        self,
        dev_id: str,
        full_name: str | None = None,
        channels: list[dict] | None = None,
        event_subscriptions: list[str] | None = None,
    ) -> str:
        """UPSERT a dev into devbrain.devs. Returns the row id as a string.

        On conflict, channels/event_subscriptions/updated_at are updated.
        full_name is preserved when the new value is None.
        """
        channels = channels or []
        event_subscriptions = (
            event_subscriptions
            if event_subscriptions is not None
            else list(self.DEFAULT_EVENT_SUBSCRIPTIONS)
        )

        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO devbrain.devs
                    (dev_id, full_name, channels, event_subscriptions)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (dev_id) DO UPDATE SET
                    full_name = COALESCE(EXCLUDED.full_name, devbrain.devs.full_name),
                    channels = EXCLUDED.channels,
                    event_subscriptions = EXCLUDED.event_subscriptions,
                    updated_at = now()
                RETURNING id
                """,
                (
                    dev_id,
                    full_name,
                    json.dumps(channels),
                    json.dumps(event_subscriptions),
                ),
            )
            row_id = str(cur.fetchone()[0])
            conn.commit()
            return row_id

    def get_dev(self, dev_id: str) -> dict | None:
        """Return a dev as a dict, or None if not found."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, dev_id, full_name, channels, event_subscriptions,
                       created_at, updated_at
                FROM devbrain.devs
                WHERE dev_id = %s
                """,
                (dev_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": str(row[0]),
                "dev_id": row[1],
                "full_name": row[2],
                "channels": row[3] or [],
                "event_subscriptions": row[4] or [],
                "created_at": row[5],
                "updated_at": row[6],
            }

    def list_devs(self) -> list[dict]:
        """Return all devs ordered by dev_id."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, dev_id, full_name, channels, event_subscriptions,
                       created_at, updated_at
                FROM devbrain.devs
                ORDER BY dev_id ASC
                """
            )
            return [
                {
                    "id": str(r[0]),
                    "dev_id": r[1],
                    "full_name": r[2],
                    "channels": r[3] or [],
                    "event_subscriptions": r[4] or [],
                    "created_at": r[5],
                    "updated_at": r[6],
                }
                for r in cur.fetchall()
            ]

    def add_dev_channel(self, dev_id: str, channel: dict) -> None:
        """Append a channel to the dev's channel list, de-duplicating on type+address."""
        dev = self.get_dev(dev_id)
        if not dev:
            raise ValueError(f"Dev '{dev_id}' not found")

        new_type = channel.get("type")
        new_address = channel.get("address")

        filtered = [
            c
            for c in dev["channels"]
            if not (c.get("type") == new_type and c.get("address") == new_address)
        ]
        filtered.append(channel)

        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE devbrain.devs
                   SET channels = %s,
                       updated_at = now()
                 WHERE dev_id = %s
                """,
                (json.dumps(filtered), dev_id),
            )
            conn.commit()

    def remove_dev_channel(
        self,
        dev_id: str,
        channel_type: str,
        address: str | None = None,
    ) -> None:
        """Remove channels matching channel_type (and optionally address)."""
        dev = self.get_dev(dev_id)
        if not dev:
            raise ValueError(f"Dev '{dev_id}' not found")

        def keep(c: dict) -> bool:
            if c.get("type") != channel_type:
                return True
            if address is not None and c.get("address") != address:
                return True
            return False

        filtered = [c for c in dev["channels"] if keep(c)]

        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE devbrain.devs
                   SET channels = %s,
                       updated_at = now()
                 WHERE dev_id = %s
                """,
                (json.dumps(filtered), dev_id),
            )
            conn.commit()

    def record_notification(
        self,
        recipient_dev_id: str,
        event_type: str,
        title: str,
        body: str,
        job_id: str | None = None,
        channels_attempted: list | None = None,
        channels_delivered: list | None = None,
        delivery_errors: dict | None = None,
        metadata: dict | None = None,
    ) -> str:
        """Insert a notification record and return its id."""
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO devbrain.notifications
                    (recipient_dev_id, job_id, event_type, title, body,
                     channels_attempted, channels_delivered, delivery_errors,
                     metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    recipient_dev_id,
                    job_id,
                    event_type,
                    title,
                    body,
                    json.dumps(channels_attempted or []),
                    json.dumps(channels_delivered or []),
                    json.dumps(delivery_errors or {}),
                    json.dumps(metadata or {}),
                ),
            )
            notification_id = str(cur.fetchone()[0])
            conn.commit()
            return notification_id

    def get_notifications(
        self,
        recipient_dev_id: str | None = None,
        job_id: str | None = None,
        event_type: str | None = None,
        since_hours: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        """Return notifications matching the given filters, newest first."""
        conditions: list[str] = []
        params: list = []

        if recipient_dev_id is not None:
            conditions.append("recipient_dev_id = %s")
            params.append(recipient_dev_id)
        if job_id is not None:
            conditions.append("job_id = %s")
            params.append(job_id)
        if event_type is not None:
            conditions.append("event_type = %s")
            params.append(event_type)
        if since_hours is not None:
            conditions.append("sent_at >= now() - (%s || ' hours')::interval")
            params.append(str(since_hours))

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(limit)

        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id, recipient_dev_id, job_id, event_type, title, body,
                       channels_attempted, channels_delivered, delivery_errors,
                       sent_at, metadata
                FROM devbrain.notifications
                {where}
                ORDER BY sent_at DESC
                LIMIT %s
                """,
                params,
            )
            return [
                {
                    "id": str(r[0]),
                    "recipient_dev_id": r[1],
                    "job_id": str(r[2]) if r[2] else None,
                    "event_type": r[3],
                    "title": r[4],
                    "body": r[5],
                    "channels_attempted": r[6] or [],
                    "channels_delivered": r[7] or [],
                    "delivery_errors": r[8] or {},
                    "sent_at": r[9],
                    "metadata": r[10] or {},
                }
                for r in cur.fetchall()
            ]
