"""End-to-end integration tests for notifications."""
import pytest
from unittest.mock import patch
from state_machine import FactoryDB, JobStatus
from cleanup_agent import CleanupAgent
from file_registry import FileRegistry
from notifications.router import NotificationRouter, NotificationEvent

from config import DATABASE_URL


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture(autouse=True)
def cleanup(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM devbrain.file_locks WHERE dev_id LIKE 'test_integ_%'")
        cur.execute("DELETE FROM devbrain.notifications WHERE recipient_dev_id LIKE 'test_integ_%'")
        cur.execute("DELETE FROM devbrain.devs WHERE dev_id LIKE 'test_integ_%'")
        # Clean up any test jobs and dependent rows
        cur.execute("""
            SELECT id FROM devbrain.factory_jobs
            WHERE submitted_by LIKE 'test_integ_%'
               OR title LIKE '%integ_notif_%'
        """)
        job_ids = [r[0] for r in cur.fetchall()]
        if job_ids:
            cur.execute(
                "DELETE FROM devbrain.factory_cleanup_reports WHERE job_id = ANY(%s)",
                (job_ids,),
            )
            cur.execute(
                "DELETE FROM devbrain.factory_artifacts WHERE job_id = ANY(%s)",
                (job_ids,),
            )
            cur.execute(
                "DELETE FROM devbrain.factory_jobs WHERE id = ANY(%s)",
                (job_ids,),
            )
        conn.commit()


def _set_submitted_by(db, job_id, dev_id):
    """Helper to set submitted_by on a test job."""
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE devbrain.factory_jobs SET submitted_by = %s WHERE id = %s",
            (dev_id, job_id),
        )
        conn.commit()


# ─── Test 1: Cleanup agent fires job_failed notification ─────────────

def test_cleanup_agent_fires_job_failed_notification(db):
    db.register_dev(
        dev_id="test_integ_alice",
        full_name="Alice Integ",
        channels=[{"type": "tmux", "address": "test_integ_alice"}],
    )

    job_id = db.create_job(
        project_slug="devbrain",
        title="integ_notif_failing_job",
        spec="Test",
    )
    _set_submitted_by(db, job_id, "test_integ_alice")

    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.FAILED)

    agent = CleanupAgent(db)
    with patch("notifications.channels.tmux.TmuxChannel._is_session_active", return_value=False):
        agent.run_post_cleanup(job_id)

    notifs = db.get_notifications(recipient_dev_id="test_integ_alice", limit=5)
    assert len(notifs) >= 1
    assert any(n["event_type"] == "job_failed" for n in notifs)
    assert any("integ_notif_failing_job" in n["title"] for n in notifs)


# ─── Test 2: Cleanup agent does NOT fire job_ready (orchestrator owns it) ──

def test_cleanup_agent_does_not_fire_job_ready_notification(db):
    """The job_ready event is emitted by orchestrator._run_qa on the
    QA → READY_FOR_APPROVAL transition. run_post_cleanup must NOT fire
    a second job_ready for the same job — that produced the duplicate
    notification ~176ms apart that this test now locks against.
    """
    db.register_dev(
        dev_id="test_integ_bob",
        channels=[{"type": "tmux", "address": "test_integ_bob"}],
    )

    job_id = db.create_job(
        project_slug="devbrain",
        title="integ_notif_ready_job",
        spec="Test",
    )
    _set_submitted_by(db, job_id, "test_integ_bob")

    # Walk through the pipeline to ready_for_approval
    db.transition(job_id, JobStatus.PLANNING)
    db.transition(job_id, JobStatus.IMPLEMENTING)
    db.transition(job_id, JobStatus.REVIEWING)
    db.transition(job_id, JobStatus.QA)
    db.transition(job_id, JobStatus.READY_FOR_APPROVAL)

    agent = CleanupAgent(db)
    with patch("notifications.channels.tmux.TmuxChannel._is_session_active", return_value=False):
        agent.run_post_cleanup(job_id)

    notifs = db.get_notifications(recipient_dev_id="test_integ_bob", limit=5)
    assert not any(n["event_type"] == "job_ready" for n in notifs), (
        f"cleanup_agent must not emit job_ready (orchestrator owns that "
        f"event). Got: {[n['event_type'] for n in notifs]}"
    )


# ─── Test 3: Lock conflict notifies both devs ────────────────────────

def test_lock_conflict_notifies_blocked_and_blocking(db):
    db.register_dev(
        dev_id="test_integ_blocked",
        channels=[{"type": "tmux", "address": "test_integ_blocked"}],
    )
    db.register_dev(
        dev_id="test_integ_blocker",
        channels=[{"type": "tmux", "address": "test_integ_blocker"}],
    )

    # Create a router and fire a lock_conflict event
    router = NotificationRouter(db, config={
        "notify_events": ["lock_conflict"],
        "channels": {"tmux": {"enabled": True}},
    })

    event = NotificationEvent(
        event_type="lock_conflict",
        recipient_dev_id="test_integ_blocked",
        title="Blocked on shared file",
        body="Job blocked by another dev",
        metadata={"blocking_dev_id": "test_integ_blocker"},
    )

    with patch("notifications.channels.tmux.TmuxChannel._is_session_active", return_value=False):
        router.send_multi(event)

    blocked_notifs = db.get_notifications(recipient_dev_id="test_integ_blocked", limit=5)
    blocker_notifs = db.get_notifications(recipient_dev_id="test_integ_blocker", limit=5)

    assert len(blocked_notifs) >= 1
    assert len(blocker_notifs) >= 1
    # The blocker gets a different title
    assert any("blocking" in n["title"].lower() for n in blocker_notifs)


# ─── Test 4: Router respects per-dev event subscriptions ─────────────

def test_per_dev_event_subscriptions_filter(db):
    # Dev only subscribed to job_failed, not job_ready
    db.register_dev(
        dev_id="test_integ_selective",
        channels=[{"type": "tmux", "address": "test_integ_selective"}],
        event_subscriptions=["job_failed"],
    )

    router = NotificationRouter(db, config={
        "notify_events": ["job_ready", "job_failed"],
        "channels": {"tmux": {"enabled": True}},
    })

    # Send a job_ready event — should be skipped
    ready_event = NotificationEvent(
        event_type="job_ready",
        recipient_dev_id="test_integ_selective",
        title="Ready",
        body="body",
    )
    with patch("notifications.channels.tmux.TmuxChannel._is_session_active", return_value=False):
        result = router.send(ready_event)
    assert result.skipped is True

    # Send a job_failed event — should go through
    failed_event = NotificationEvent(
        event_type="job_failed",
        recipient_dev_id="test_integ_selective",
        title="Failed",
        body="body",
    )
    with patch("notifications.channels.tmux.TmuxChannel._is_session_active", return_value=False):
        result2 = router.send(failed_event)
    assert result2.skipped is False


# ─── Test 5: Router iterates multiple channels per dev ──────────────

def test_router_attempts_multiple_channels(db):
    db.register_dev(
        dev_id="test_integ_multi",
        channels=[
            {"type": "tmux", "address": "test_integ_multi"},
            {"type": "webhook_slack", "address": "https://hooks.slack.com/services/fake"},
        ],
    )

    router = NotificationRouter(db, config={
        "notify_events": ["job_ready"],
        "channels": {
            "tmux": {"enabled": True},
            "webhook_slack": {"enabled": True},
        },
    })

    event = NotificationEvent(
        event_type="job_ready",
        recipient_dev_id="test_integ_multi",
        title="Multi channel",
        body="body",
    )

    with patch("notifications.channels.tmux.TmuxChannel._is_session_active", return_value=False):
        with patch("urllib.request.urlopen") as mock_urlopen:
            from unittest.mock import MagicMock
            # Mock the slack webhook to return success
            mock_resp = MagicMock()
            mock_resp.read.return_value = b"ok"
            mock_resp.getcode.return_value = 200
            mock_resp.__enter__ = lambda self: self
            mock_resp.__exit__ = lambda *args: None
            mock_urlopen.return_value = mock_resp

            result = router.send(event)

    # Both channels should have been attempted
    assert "tmux" in result.channels_attempted
    assert "webhook_slack" in result.channels_attempted
    # Slack should have been delivered (mock returned 200)
    assert "webhook_slack" in result.channels_delivered


# ─── Test 6: job_started notification fires when pipeline begins ──────

def test_job_started_notification_fires_from_orchestrator(db):
    """Running a job through _run_planning fires job_started."""
    from orchestrator import FactoryOrchestrator

    db.register_dev(
        dev_id="test_integ_starter",
        channels=[{"type": "tmux", "address": "test_integ_starter"}],
    )

    job_id = db.create_job(
        project_slug="devbrain",
        title="integ_notif_started_job",
        spec="Test",
    )
    _set_submitted_by(db, job_id, "test_integ_starter")

    orch = FactoryOrchestrator(DATABASE_URL)

    # Mock the CLI call in planning so it doesn't actually run claude
    from unittest.mock import MagicMock
    fake_result = MagicMock()
    fake_result.stdout = "Fake plan"
    fake_result.stderr = ""
    fake_result.success = False  # Stop pipeline after planning (transitions to FAILED)

    with patch("orchestrator.run_cli", return_value=fake_result):
        with patch("notifications.channels.tmux.TmuxChannel._is_session_active", return_value=False):
            # Reload job to get current state after our SQL update
            job = db.get_job(job_id)
            orch._run_planning(job)

    notifs = db.get_notifications(recipient_dev_id="test_integ_starter", limit=10)
    assert any(n["event_type"] == "job_started" for n in notifs), (
        f"Expected job_started notification, got: {[n['event_type'] for n in notifs]}"
    )


# ─── Test 7: recovery_started fires at start of recovery attempt ──────

def test_recovery_started_notification(db):
    """attempt_recovery fires recovery_started at the start."""
    db.register_dev(
        dev_id="test_integ_recstart",
        channels=[{"type": "tmux", "address": "test_integ_recstart"}],
    )

    job_id = db.create_job(
        project_slug="devbrain",
        title="integ_notif_recovery_started_job",
        spec="Test",
    )
    _set_submitted_by(db, job_id, "test_integ_recstart")

    # Set up a job in a state where recovery would fire needs_human (no fix_loop arts)
    db.transition(job_id, JobStatus.PLANNING)

    agent = CleanupAgent(db)
    job = db.get_job(job_id)

    with patch("notifications.channels.tmux.TmuxChannel._is_session_active", return_value=False):
        agent.attempt_recovery(job)

    notifs = db.get_notifications(recipient_dev_id="test_integ_recstart", limit=10)
    assert any(n["event_type"] == "recovery_started" for n in notifs), (
        f"Expected recovery_started, got: {[n['event_type'] for n in notifs]}"
    )


# ─── Test 8: recovery_succeeded fires on successful fix ──────────────

def test_recovery_succeeded_notification(db):
    """attempt_recovery fires recovery_succeeded when the targeted fix succeeds."""
    db.register_dev(
        dev_id="test_integ_recwin",
        channels=[{"type": "tmux", "address": "test_integ_recwin"}],
    )

    job_id = db.create_job(
        project_slug="devbrain",
        title="integ_notif_recovery_win_job",
        spec="Test",
    )
    _set_submitted_by(db, job_id, "test_integ_recwin")

    # Create artifacts that make _diagnose_failure return qa_failure + converging
    db.transition(job_id, JobStatus.PLANNING)
    db.store_artifact(job_id, "reviewing", "review", "blocking stuff", blocking_count=5)
    db.store_artifact(job_id, "fix_loop", "fix", "fix attempt 1")
    db.store_artifact(job_id, "reviewing", "review", "less blocking", blocking_count=2)

    agent = CleanupAgent(db)
    job = db.get_job(job_id)

    # Mock _attempt_targeted_fix to return success=True
    with patch.object(agent, "_attempt_targeted_fix", return_value=(True, "Fix applied successfully")):
        with patch("notifications.channels.tmux.TmuxChannel._is_session_active", return_value=False):
            report = agent.attempt_recovery(job)

    assert report.outcome == "recovered"

    notifs = db.get_notifications(recipient_dev_id="test_integ_recwin", limit=10)
    event_types = [n["event_type"] for n in notifs]
    assert "recovery_started" in event_types
    assert "recovery_succeeded" in event_types
