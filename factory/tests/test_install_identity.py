"""Tests for setup.install_identity — non-interactive default dev registration."""
import pytest

import setup
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
    monkeypatch.setattr(FactoryDB, "get_dev", lambda self, dev_id: None)
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
    monkeypatch.setattr(FactoryDB, "get_dev", lambda self, dev_id: None)
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


def test_install_identity_preserves_existing_row(monkeypatch):
    """If a row already exists for dev_id, register_dev is NOT called.

    This protects user-customized channels and event_subscriptions from
    being overwritten on re-runs of install.sh.
    """
    register_calls = []

    def fake_register_dev(self, *args, **kwargs):
        register_calls.append((args, kwargs))
        return "row-id"

    existing = {
        "id": "row-id",
        "dev_id": "test_install_identity_existing",
        "full_name": "Existing User",
        "channels": [{"type": "slack", "target": "#alerts"}],
        "event_subscriptions": ["job_blocked"],
    }
    monkeypatch.setattr(FactoryDB, "register_dev", fake_register_dev)
    monkeypatch.setattr(
        FactoryDB, "get_dev",
        lambda self, dev_id: existing if dev_id == existing["dev_id"] else None,
    )

    result = setup.install_identity(dev_id="test_install_identity_existing")

    assert result == "test_install_identity_existing"
    assert register_calls == []


# ─── Integration tests (real DB) ───────────────────────────────────────────
#
# `db` fixture connects + purges; mock tests above don't request it, so they
# never hit Postgres.

@pytest.fixture
def db(database_url):
    conn_db = FactoryDB(database_url)

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


def test_install_identity_persists_row(db, database_url, monkeypatch):
    """End-to-end: row is written and readable via get_dev."""
    # setup.install_identity() creates its own FactoryDB(DATABASE_URL) internally;
    # monkeypatch ensures it uses the test DB URL, not the config default.
    monkeypatch.setattr("setup.DATABASE_URL", database_url)
    dev_id = "test_install_identity_persist"
    result = setup.install_identity(dev_id=dev_id)

    assert result == dev_id
    row = db.get_dev(dev_id)
    assert row is not None
    assert row["dev_id"] == dev_id
    assert row["full_name"] is None
    assert row["channels"] == []


def test_install_identity_idempotent(db, database_url, monkeypatch):
    """Re-running with the same dev_id does not error or duplicate."""
    # setup.install_identity() creates its own FactoryDB(DATABASE_URL) internally;
    # monkeypatch ensures it uses the test DB URL, not the config default.
    monkeypatch.setattr("setup.DATABASE_URL", database_url)
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
