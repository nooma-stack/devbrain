"""Tests for devs + notifications CRUD on FactoryDB."""
import pytest
from state_machine import FactoryDB

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture(autouse=True)
def cleanup(db):
    """Cleanup any test_* prefixed devs and notifications before and after each test."""
    def _purge():
        with db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM devbrain.notifications WHERE recipient_dev_id LIKE 'test_%%'"
            )
            cur.execute(
                "DELETE FROM devbrain.devs WHERE dev_id LIKE 'test_%%'"
            )
            conn.commit()

    _purge()
    yield
    _purge()


def test_register_dev_with_channels(db):
    """Registering a dev with channels stores them as JSON."""
    dev_id = "test_alice"
    channels = [
        {"type": "tmux", "address": "alice"},
        {"type": "email", "address": "alice@example.com"},
    ]
    returned_id = db.register_dev(
        dev_id, full_name="Alice Example", channels=channels
    )
    assert returned_id

    dev = db.get_dev(dev_id)
    assert dev is not None
    assert dev["dev_id"] == dev_id
    assert dev["full_name"] == "Alice Example"
    assert dev["channels"] == channels
    # Default event_subscriptions applied
    assert "job_ready" in dev["event_subscriptions"]
    assert "needs_human" in dev["event_subscriptions"]


def test_register_dev_upsert(db):
    """Re-registering updates channels and event_subscriptions."""
    dev_id = "test_bob"
    db.register_dev(dev_id, full_name="Bob", channels=[{"type": "tmux", "address": "bob"}])

    # Re-register with new channels, no full_name
    db.register_dev(
        dev_id,
        channels=[{"type": "email", "address": "bob@example.com"}],
        event_subscriptions=["job_ready"],
    )

    dev = db.get_dev(dev_id)
    assert dev["full_name"] == "Bob"  # preserved
    assert dev["channels"] == [{"type": "email", "address": "bob@example.com"}]
    assert dev["event_subscriptions"] == ["job_ready"]


def test_add_dev_channel(db):
    """Adding a new channel appends without replacing others."""
    dev_id = "test_carol"
    db.register_dev(
        dev_id, channels=[{"type": "tmux", "address": "carol"}]
    )
    db.add_dev_channel(dev_id, {"type": "email", "address": "carol@example.com"})

    dev = db.get_dev(dev_id)
    assert len(dev["channels"]) == 2
    types = {c["type"] for c in dev["channels"]}
    assert types == {"tmux", "email"}


def test_add_dev_channel_deduplicates(db):
    """Adding a channel with the same type+address replaces instead of duplicating."""
    dev_id = "test_dan"
    db.register_dev(
        dev_id, channels=[{"type": "tmux", "address": "dan", "extra": "old"}]
    )
    db.add_dev_channel(dev_id, {"type": "tmux", "address": "dan", "extra": "new"})

    dev = db.get_dev(dev_id)
    assert len(dev["channels"]) == 1
    assert dev["channels"][0]["extra"] == "new"


def test_add_dev_channel_missing_dev(db):
    with pytest.raises(ValueError):
        db.add_dev_channel("test_ghost", {"type": "tmux", "address": "x"})


def test_remove_dev_channel(db):
    """Removing by type removes all matching channels."""
    dev_id = "test_eve"
    db.register_dev(
        dev_id,
        channels=[
            {"type": "tmux", "address": "eve"},
            {"type": "email", "address": "eve@example.com"},
        ],
    )
    db.remove_dev_channel(dev_id, "tmux")

    dev = db.get_dev(dev_id)
    assert len(dev["channels"]) == 1
    assert dev["channels"][0]["type"] == "email"


def test_remove_dev_channel_with_address(db):
    """Removing with an address only removes matches for that address."""
    dev_id = "test_frank"
    db.register_dev(
        dev_id,
        channels=[
            {"type": "email", "address": "a@x.com"},
            {"type": "email", "address": "b@x.com"},
        ],
    )
    db.remove_dev_channel(dev_id, "email", address="a@x.com")
    dev = db.get_dev(dev_id)
    assert len(dev["channels"]) == 1
    assert dev["channels"][0]["address"] == "b@x.com"


def test_remove_dev_channel_missing_dev(db):
    with pytest.raises(ValueError):
        db.remove_dev_channel("test_ghost", "tmux")


def test_get_nonexistent_dev(db):
    assert db.get_dev("test_nobody") is None


def test_list_devs(db):
    db.register_dev("test_aa", channels=[{"type": "tmux", "address": "aa"}])
    db.register_dev("test_bb", channels=[{"type": "tmux", "address": "bb"}])
    devs = db.list_devs()
    dev_ids = [d["dev_id"] for d in devs]
    assert "test_aa" in dev_ids
    assert "test_bb" in dev_ids


def test_record_and_get_notification(db):
    db.register_dev("test_recipient", channels=[{"type": "tmux", "address": "r"}])
    nid = db.record_notification(
        recipient_dev_id="test_recipient",
        event_type="job_ready",
        title="Job ready",
        body="Your job is ready",
        channels_attempted=[{"type": "tmux", "address": "r"}],
        channels_delivered=[{"type": "tmux", "address": "r"}],
        delivery_errors={},
        metadata={"job": "x"},
    )
    assert nid

    notes = db.get_notifications(recipient_dev_id="test_recipient")
    assert len(notes) == 1
    n = notes[0]
    assert n["title"] == "Job ready"
    assert n["event_type"] == "job_ready"
    assert n["channels_delivered"] == [{"type": "tmux", "address": "r"}]
    assert n["metadata"] == {"job": "x"}
    assert isinstance(n["id"], str)


def test_get_notifications_filtered_by_event_type(db):
    db.register_dev("test_recipient2", channels=[])
    db.record_notification(
        recipient_dev_id="test_recipient2",
        event_type="job_ready",
        title="Ready",
        body="ready body",
    )
    db.record_notification(
        recipient_dev_id="test_recipient2",
        event_type="job_failed",
        title="Failed",
        body="failed body",
    )

    ready = db.get_notifications(
        recipient_dev_id="test_recipient2", event_type="job_ready"
    )
    assert len(ready) == 1
    assert ready[0]["event_type"] == "job_ready"

    failed = db.get_notifications(
        recipient_dev_id="test_recipient2", event_type="job_failed"
    )
    assert len(failed) == 1
    assert failed[0]["event_type"] == "job_failed"


def test_get_notifications_limit(db):
    db.register_dev("test_recipient3", channels=[])
    for i in range(5):
        db.record_notification(
            recipient_dev_id="test_recipient3",
            event_type="job_ready",
            title=f"Msg {i}",
            body="body",
        )
    notes = db.get_notifications(recipient_dev_id="test_recipient3", limit=3)
    assert len(notes) == 3
