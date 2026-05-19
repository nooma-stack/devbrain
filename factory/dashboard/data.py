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

    # Pass schedule (seconds between runs). Used to derive `down` state
    # — if no run has happened within `expected_interval * DOWN_FACTOR`,
    # we consider the launchd job stopped. Matches the StartInterval
    # values in factory/cognify/launchd/com.devbrain.cognify-*.plist.
    _COGNIFY_PASS_SCHEDULE: dict[str, int] = {
        "extract":    3_600,   # hourly
        "edges":      21_600,  # every 6h
        "decay":      3_600,   # hourly
        "strengthen": 86_400,  # daily
        "gc":         604_800, # weekly
    }
    _DOWN_FACTOR = 3  # >3× expected interval since last run = consider down

    def get_cognify_pass_status(self, project: str | None = None) -> list[dict]:
        """Return per-pass status rows for the cognify dashboard panel.

        Pulls the most-recent row per pass_name from cognify_run_log and
        derives a state from its timing + completion + error fields:

          * `running`   — most recent row has started_at but no completed_at
                          (the pass is currently in flight).
          * `errored`   — most recent COMPLETED row has a non-null error.
          * `idle`      — last successful run is within expected_interval;
                          launchd is firing as expected.
          * `down`      — last run is older than expected_interval * DOWN_FACTOR.
                          Suggests launchd isn't firing (plist unloaded, etc.).
          * `never_run` — no row in cognify_run_log for this pass.

        Returns one dict per pass with keys: pass_name, state, last_run,
        last_completed, last_rows_processed, last_llm_calls, last_error
        (truncated), project (slug or None for global passes).

        `project` filter scopes to a specific project_id. Global passes
        (decay, gc — project_id IS NULL) are always included.
        """
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        rows = []

        # Project filter resolves to project_id (or None if not given).
        project_id = None
        if project:
            with self.db._conn() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM devbrain.projects WHERE slug = %s",
                    (project,),
                )
                row = cur.fetchone()
                project_id = row[0] if row else None

        with self.db._conn() as conn, conn.cursor() as cur:
            # For each known pass, fetch the most recent row.
            for pass_name in self._COGNIFY_PASS_SCHEDULE:
                if project_id is not None:
                    # Project-scoped passes (extract, edges, strengthen)
                    # filter to project_id; global passes (decay, gc)
                    # have project_id IS NULL.
                    cur.execute(
                        """
                        SELECT started_at, completed_at, rows_processed,
                               llm_calls, error
                        FROM devbrain.cognify_run_log
                        WHERE pass_name = %s
                          AND (project_id = %s OR project_id IS NULL)
                        ORDER BY started_at DESC
                        LIMIT 1
                        """,
                        (pass_name, project_id),
                    )
                else:
                    cur.execute(
                        """
                        SELECT started_at, completed_at, rows_processed,
                               llm_calls, error
                        FROM devbrain.cognify_run_log
                        WHERE pass_name = %s
                        ORDER BY started_at DESC
                        LIMIT 1
                        """,
                        (pass_name,),
                    )
                last = cur.fetchone()
                expected = self._COGNIFY_PASS_SCHEDULE[pass_name]

                if last is None:
                    rows.append({
                        "pass_name": pass_name,
                        "state": "never_run",
                        "last_run": None,
                        "last_completed": None,
                        "last_rows_processed": 0,
                        "last_llm_calls": 0,
                        "last_error": None,
                        "expected_interval_s": expected,
                    })
                    continue

                started_at, completed_at, rows_processed, llm_calls, error = last

                # State derivation
                if completed_at is None:
                    state = "running"
                elif error is not None:
                    state = "errored"
                else:
                    age = (now - completed_at).total_seconds()
                    if age > expected * self._DOWN_FACTOR:
                        state = "down"
                    else:
                        state = "idle"

                rows.append({
                    "pass_name": pass_name,
                    "state": state,
                    "last_run": started_at,
                    "last_completed": completed_at,
                    "last_rows_processed": rows_processed or 0,
                    "last_llm_calls": llm_calls or 0,
                    "last_error": (error[:200] if error else None),
                    "expected_interval_s": expected,
                })

        return rows

    def get_recent_sessions(
        self, project: str | None = None, limit: int = 10,
    ) -> list[dict]:
        """Recent end_session_log rows for the Active Sessions panel.

        Issue #135 — surfaces (session_id, dev_id, cli, project_slug,
        applied_at) so the dashboard can show "who ended which session
        on which CLI, when." Rows with NULL dev_id/cli are pre-038 or
        local (non-SSH) sessions; they render as "—" so the gap is
        visible rather than silently inferred.

        Dedupes by session_id (returning the most-recent payload_hash
        per session) because a session_id can show up twice when the
        agent retries end_session with a different payload — the
        enrichment-bearing payload is the one we want to surface.

        `project` is a slug filter; None means "all projects."
        """
        project_filter = ""
        params: list = []
        if project:
            project_filter = "AND p.slug = %s"
            params.append(project)

        params.append(limit)
        sql = f"""
        SELECT DISTINCT ON (esl.session_id)
            esl.session_id, esl.dev_id, esl.cli, esl.applied_at,
            p.slug AS project_slug
        FROM devbrain.end_session_log esl
        JOIN devbrain.projects p ON p.id = esl.project_id
        WHERE TRUE {project_filter}
        ORDER BY esl.session_id, esl.applied_at DESC
        """
        # Wrap to re-order by applied_at globally after the DISTINCT ON
        # collapse. DISTINCT ON requires ORDER BY (session_id, ...) so
        # we can't sort globally inside the same query.
        sql = f"""
        SELECT * FROM (
            {sql}
        ) deduped
        ORDER BY applied_at DESC
        LIMIT %s
        """

        rows: list[dict] = []
        with self.db._conn() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            for r in cur.fetchall():
                rows.append({
                    "session_id": r[0],
                    "dev_id": r[1],
                    "cli": r[2],
                    "applied_at": r[3],
                    "project_slug": r[4],
                })
        return rows
