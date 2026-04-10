"""Tests for NotificationRouter."""
import pytest
from unittest.mock import patch
from state_machine import FactoryDB
from notifications.router import NotificationRouter, NotificationEvent

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture(autouse=True)
def cleanup(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM devbrain.notifications WHERE recipient_dev_id LIKE 'test_router_%'")
        cur.execute("DELETE FROM devbrain.devs WHERE dev_id LIKE 'test_router_%'")
        conn.commit()


@pytest.fixture
def router(db):
    return NotificationRouter(db, config={
        "notify_events": ["job_ready", "job_failed", "lock_conflict"],
        "channels": {
            "tmux": {"enabled": True},
        },
    })


def test_router_skips_unsubscribed_event_types(router, db):
    db.register_dev(
        dev_id="test_router_skip",
        channels=[{"type": "tmux", "address": "test_router_skip"}],
    )
    event = NotificationEvent(
        event_type="phase_transition",  # Not in notify_events
        recipient_dev_id="test_router_skip",
        title="Phase",
        body="body",
    )
    result = router.send(event)
    assert result.skipped is True


def test_router_records_notification_with_failed_delivery(router, db):
    db.register_dev(
        dev_id="test_router_rec",
        channels=[{"type": "tmux", "address": "test_router_rec"}],
    )
    event = NotificationEvent(
        event_type="job_ready",
        recipient_dev_id="test_router_rec",
        title="Test ready",
        body="body",
    )
    # Tmux will fail because no session exists — but notification is still recorded
    with patch("notifications.channels.tmux.TmuxChannel._is_session_active", return_value=False):
        router.send(event)

    notifs = db.get_notifications(recipient_dev_id="test_router_rec", limit=5)
    assert len(notifs) >= 1
    assert notifs[0]["title"] == "Test ready"


def test_router_iterates_all_dev_channels(router, db):
    db.register_dev(
        dev_id="test_router_multi",
        channels=[
            {"type": "tmux", "address": "test_router_multi"},
            {"type": "smtp", "address": "foo@example.com"},  # smtp disabled globally in router config
        ],
    )
    event = NotificationEvent(
        event_type="job_failed",
        recipient_dev_id="test_router_multi",
        title="Failed",
        body="body",
    )
    with patch("notifications.channels.tmux.TmuxChannel._is_session_active", return_value=False):
        result = router.send(event)

    # Only tmux should be attempted (smtp disabled)
    assert "tmux" in result.channels_attempted
    assert "smtp" not in result.channels_attempted


def test_router_unregistered_dev_records_error(router, db):
    """Notification for unregistered dev is still recorded with error."""
    event = NotificationEvent(
        event_type="job_ready",
        recipient_dev_id="test_router_ghost",
        title="Ghost",
        body="body",
    )
    router.send(event)
    notifs = db.get_notifications(recipient_dev_id="test_router_ghost", limit=5)
    assert len(notifs) >= 1
    assert "not registered" in str(notifs[0]["delivery_errors"])


def test_lock_conflict_notifies_both_devs(db):
    router = NotificationRouter(db, config={
        "notify_events": ["lock_conflict"],
        "channels": {"tmux": {"enabled": True}},
    })
    db.register_dev(
        dev_id="test_router_blocked",
        channels=[{"type": "tmux", "address": "test_router_blocked"}],
    )
    db.register_dev(
        dev_id="test_router_blocker",
        channels=[{"type": "tmux", "address": "test_router_blocker"}],
    )

    event = NotificationEvent(
        event_type="lock_conflict",
        recipient_dev_id="test_router_blocked",
        title="File conflict",
        body="Blocked by blocker on src/shared.py",
        metadata={"blocking_dev_id": "test_router_blocker"},
    )

    with patch("notifications.channels.tmux.TmuxChannel._is_session_active", return_value=False):
        results = router.send_multi(event)

    blocked = db.get_notifications(recipient_dev_id="test_router_blocked", limit=5)
    blocker = db.get_notifications(recipient_dev_id="test_router_blocker", limit=5)
    assert len(blocked) >= 1
    assert len(blocker) >= 1
    # Blocker gets a different title
    assert "blocking" in blocker[0]["title"].lower()
