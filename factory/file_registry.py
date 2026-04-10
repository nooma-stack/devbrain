"""File lock registry for multi-dev concurrency control.

Coordinates file access between parallel factory jobs. Each lock is scoped
to (project_id, file_path) and owned by a job_id. Locks expire after a
default interval (controlled by the file_locks table default) so crashed
or abandoned jobs don't block others forever.

Usage::

    registry = FileRegistry(db)
    result = registry.acquire_locks(job_id, project_id, ["src/foo.py"], dev_id="alice")
    if not result.success:
        for conflict in result.conflicts:
            ...  # another job owns this file
    # ... do work ...
    registry.release_locks(job_id)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from state_machine import FactoryDB

logger = logging.getLogger(__name__)


@dataclass
class LockConflict:
    """Describes a single file lock conflict."""

    file_path: str
    blocking_job_id: str
    blocking_dev_id: str | None
    locked_at: datetime


@dataclass
class LockResult:
    """Result of an acquire_locks call."""

    success: bool
    conflicts: list[dict] = field(default_factory=list)
    acquired_count: int = 0


class LockConflictError(Exception):
    """Raised when callers want an exception-based API for lock conflicts."""

    def __init__(self, conflicts: list[dict]):
        self.conflicts = conflicts
        paths = ", ".join(c["file_path"] for c in conflicts)
        super().__init__(f"File lock conflict on: {paths}")


class FileRegistry:
    """Manages file locks in the devbrain.file_locks table."""

    def __init__(self, db: FactoryDB):
        self.db = db

    def acquire_locks(
        self,
        job_id: str,
        project_id: str,
        file_paths: list[str],
        dev_id: str | None = None,
    ) -> LockResult:
        """Attempt to acquire locks on all file_paths for job_id.

        All-or-nothing: if ANY file conflicts with another job's lock, no
        locks are acquired and the conflicts are returned in the result.

        Expired locks are cleaned up at the start of every call so stale
        locks don't block progress.
        """
        if not file_paths:
            return LockResult(success=True, conflicts=[], acquired_count=0)

        # Dedup while preserving order
        seen: set[str] = set()
        unique_paths: list[str] = []
        for path in file_paths:
            if path not in seen:
                seen.add(path)
                unique_paths.append(path)

        with self.db._conn() as conn, conn.cursor() as cur:
            # 1. Clean up expired locks first so we don't falsely block.
            cur.execute(
                "DELETE FROM devbrain.file_locks WHERE expires_at < now()"
            )
            expired_removed = cur.rowcount
            if expired_removed:
                logger.info(
                    "Cleaned up %d expired file locks before acquire",
                    expired_removed,
                )

            # 2. Check for conflicts (locks owned by OTHER jobs on same project).
            cur.execute(
                """
                SELECT file_path, job_id, dev_id, locked_at
                FROM devbrain.file_locks
                WHERE project_id = %s
                  AND file_path = ANY(%s)
                  AND job_id != %s
                """,
                (project_id, unique_paths, job_id),
            )
            conflict_rows = cur.fetchall()

            if conflict_rows:
                conflicts = [
                    {
                        "file_path": row[0],
                        "blocking_job_id": str(row[1]),
                        "blocking_dev_id": row[2],
                        "locked_at": row[3],
                    }
                    for row in conflict_rows
                ]
                conn.commit()  # commit the expired cleanup regardless
                logger.warning(
                    "Job %s failed to acquire locks: %d conflict(s) on project %s",
                    job_id[:8],
                    len(conflicts),
                    str(project_id)[:8],
                )
                return LockResult(
                    success=False, conflicts=conflicts, acquired_count=0
                )

            # 3. No conflicts — insert all locks. ON CONFLICT handles the
            #    case where the job already holds some of these locks.
            acquired = 0
            for path in unique_paths:
                cur.execute(
                    """
                    INSERT INTO devbrain.file_locks
                        (job_id, project_id, file_path, dev_id)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (project_id, file_path) DO NOTHING
                    """,
                    (job_id, project_id, path, dev_id),
                )
                if cur.rowcount:
                    acquired += 1

            conn.commit()

        logger.info(
            "Job %s acquired %d file lock(s) (dev_id=%s)",
            job_id[:8],
            acquired,
            dev_id,
        )
        return LockResult(success=True, conflicts=[], acquired_count=acquired)

    def release_locks(self, job_id: str) -> int:
        """Release all locks held by job_id. Returns count released."""
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM devbrain.file_locks WHERE job_id = %s",
                (job_id,),
            )
            count = cur.rowcount
            conn.commit()
        logger.info("Job %s released %d file lock(s)", job_id[:8], count)
        return count

    def list_locked_files(self, project_id: str) -> list[dict]:
        """List all non-expired locks for a project."""
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, job_id, project_id, file_path, dev_id,
                       locked_at, expires_at
                FROM devbrain.file_locks
                WHERE project_id = %s
                  AND expires_at >= now()
                ORDER BY locked_at ASC
                """,
                (project_id,),
            )
            return [
                {
                    "id": str(row[0]),
                    "job_id": str(row[1]),
                    "project_id": str(row[2]),
                    "file_path": row[3],
                    "dev_id": row[4],
                    "locked_at": row[5],
                    "expires_at": row[6],
                }
                for row in cur.fetchall()
            ]

    def cleanup_expired_locks(self) -> int:
        """Delete all locks whose expires_at is in the past. Returns count."""
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM devbrain.file_locks WHERE expires_at < now()"
            )
            count = cur.rowcount
            conn.commit()
        if count:
            logger.info("Cleaned up %d expired file lock(s)", count)
        return count

    def get_job_locks(self, job_id: str) -> list[str]:
        """Return the list of file paths currently locked by job_id."""
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT file_path
                FROM devbrain.file_locks
                WHERE job_id = %s
                ORDER BY file_path ASC
                """,
                (job_id,),
            )
            return [row[0] for row in cur.fetchall()]
