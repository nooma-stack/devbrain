"""Tests for setup.install_identity — non-interactive default dev registration."""
import pytest

import setup
from config import DATABASE_URL
from state_machine import FactoryDB


# ─── Mock-based tests (no DB) ──────────────────────────────────────────────

def test_install_identity_registers_with_explicit_id(monkeypatch):
    """Explicit dev_id is passed straight through to register_dev."""
    calls = []

    def fake_register_dev(self, dev_id, full_name=None, channels=None,
                          event_subscriptions=None):
        calls.append({"dev_id": dev_id, "full_name": full_name, "channels": channels})
        return "row-id"

    monkeypatch.setattr(FactoryDB, "register_dev", fake_register_dev)
    monkeypatch.delenv("USER", raising=False)

    result = setup.install_identity(dev_id="test_install_identity_explicit")

    assert result == "test_install_identity_explicit"
    assert len(calls) == 1
    assert calls[0]["dev_id"] == "test_install_identity_explicit"
    assert calls[0]["full_name"] is None
    assert calls[0]["channels"] == []


def test_install_identity_falls_back_to_user_env(monkeypatch):
    """When dev_id is None, $USER is used."""
    calls = []

    def fake_register_dev(self, dev_id, full_name=None, channels=None,
                          event_subscriptions=None):
        calls.append(dev_id)
        return "row-id"

    monkeypatch.setattr(FactoryDB, "register_dev", fake_register_dev)
    monkeypatch.setenv("USER", "test_install_identity_envuser")

    result = setup.install_identity(dev_id=None)

    assert result == "test_install_identity_envuser"
    assert calls == ["test_install_identity_envuser"]


def test_install_identity_skips_when_no_id_and_no_user(monkeypatch):
    """No --dev-id and no $USER → return None, do not call register_dev."""
    called = []

    def fake_register_dev(self, *args, **kwargs):
        called.append(True)
        return "row-id"

    monkeypatch.setattr(FactoryDB, "register_dev", fake_register_dev)
    monkeypatch.delenv("USER", raising=False)

    result = setup.install_identity(dev_id=None)

    assert result is None
    assert called == []


# ─── Integration tests (real DB) ───────────────────────────────────────────
#
# `db` fixture connects + purges; mock tests above don't request it, so they
# never hit Postgres.

@pytest.fixture
def db():
    conn_db = FactoryDB(DATABASE_URL)

    def _purge():
        with conn_db._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM devbrain.devs "
                "WHERE dev_id LIKE 'test_install_identity_%%'"
            )
            conn.commit()

    _purge()
    yield conn_db
    _purge()


def test_install_identity_persists_row(db):
    """End-to-end: row is written and readable via get_dev."""
    dev_id = "test_install_identity_persist"
    result = setup.install_identity(dev_id=dev_id)

    assert result == dev_id
    row = db.get_dev(dev_id)
    assert row is not None
    assert row["dev_id"] == dev_id
    assert row["full_name"] is None
    assert row["channels"] == []


def test_install_identity_idempotent(db):
    """Re-running with the same dev_id does not error or duplicate."""
    dev_id = "test_install_identity_idem"

    first = setup.install_identity(dev_id=dev_id)
    second = setup.install_identity(dev_id=dev_id)

    assert first == dev_id
    assert second == dev_id

    # Exactly one row exists for this dev_id.
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM devbrain.devs WHERE dev_id = %s",
            (dev_id,),
        )
        assert cur.fetchone()[0] == 1
