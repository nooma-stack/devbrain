"""Tests for `factory/invitations.py:record_oauth_token_for_dev`.

Issue #126: `claude.py:login()` wrote the OAuth token to disk without
updating `invitations.oauth_token_received_at`. The new helper closes
that gap by accepting a `dev_id` (the bootstrap token is already gone
by the time `login` runs).
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Mirror the sys.path tweak used in test_dual_write.py so that imports
# resolve when pytest is rooted at factory/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invitations import record_oauth_token_for_dev  # noqa: E402
from state_machine import FactoryDB  # noqa: E402

TEST_DEV_PREFIX = "test_oauth_receipt_"
VALID_TOKEN = "sk-ant-oat01-" + "x" * 80


@pytest.fixture
def db(database_url):
    return FactoryDB(database_url)


@pytest.fixture(autouse=True)
def _cleanup(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM devbrain.invitations WHERE dev_id LIKE %s",
            (f"{TEST_DEV_PREFIX}%",),
        )
        conn.commit()


def _seed_invitation(
    db,
    dev_id: str,
    *,
    oauth_token_received_at: datetime | None = None,
    created_at: datetime | None = None,
) -> str:
    """Insert a minimal invitation row directly. Returns the invitation id."""
    import hashlib  # noqa: PLC0415

    inv_id = str(uuid.uuid4())
    # token_hash column is CHAR(64). Synthesize a unique 64-hex value
    # by hashing (dev_id, inv_id) — keeps rows distinct without needing
    # to mint real bootstrap tokens.
    token_hash = hashlib.sha256(
        f"{dev_id}:{inv_id}".encode()
    ).hexdigest()
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO devbrain.invitations (
                id, dev_id, token_hash, status, auto_activate,
                created_at, expires_at, oauth_token_received_at
            ) VALUES (%s, %s, %s, 'activated', true,
                      COALESCE(%s, NOW()),
                      NOW() + INTERVAL '7 days',
                      %s)
            """,
            (
                inv_id, dev_id, token_hash,
                created_at, oauth_token_received_at,
            ),
        )
        conn.commit()
    return inv_id


def test_records_receipt_on_dev_with_one_pending_invitation(db):
    dev_id = TEST_DEV_PREFIX + "happy"
    _seed_invitation(db, dev_id)  # oauth_token_received_at = NULL

    invitation = record_oauth_token_for_dev(
        db, dev_id=dev_id, oauth_token=VALID_TOKEN,
    )

    assert invitation is not None
    assert invitation.dev_id == dev_id
    assert invitation.oauth_token_received_at is not None
    assert invitation.oauth_token == VALID_TOKEN


def test_returns_none_when_no_invitation_exists(db):
    invitation = record_oauth_token_for_dev(
        db, dev_id=TEST_DEV_PREFIX + "ghost", oauth_token=VALID_TOKEN,
    )
    assert invitation is None


def test_idempotent_no_double_update(db):
    dev_id = TEST_DEV_PREFIX + "idem"
    _seed_invitation(db, dev_id)

    # First call records the receipt.
    first = record_oauth_token_for_dev(db, dev_id=dev_id, oauth_token=VALID_TOKEN)
    assert first is not None
    first_timestamp = first.oauth_token_received_at

    # Second call should find no eligible row (the receipt is already set).
    second = record_oauth_token_for_dev(db, dev_id=dev_id, oauth_token=VALID_TOKEN)
    assert second is None

    # Confirm the row's timestamp wasn't moved by the second call.
    from invitations import list_invitations  # noqa: PLC0415

    rows = [i for i in list_invitations(db) if i.dev_id == dev_id]
    assert len(rows) == 1
    assert rows[0].oauth_token_received_at == first_timestamp


def test_targets_most_recent_invitation_when_multiple_eligible(db):
    """If a dev has multiple invitation rows with NULL receipt, only the
    most recently created one gets updated. (Older rows are presumably
    stale/superseded.)"""
    dev_id = TEST_DEV_PREFIX + "multi"
    older_at = datetime.now(timezone.utc) - timedelta(days=10)
    newer_at = datetime.now(timezone.utc) - timedelta(days=1)
    _seed_invitation(db, dev_id, created_at=older_at)
    newer_id = _seed_invitation(db, dev_id, created_at=newer_at)

    updated = record_oauth_token_for_dev(db, dev_id=dev_id, oauth_token=VALID_TOKEN)
    assert updated is not None
    assert str(updated.id) == newer_id


def test_rejects_malformed_token(db):
    dev_id = TEST_DEV_PREFIX + "malformed"
    _seed_invitation(db, dev_id)

    invitation = record_oauth_token_for_dev(
        db, dev_id=dev_id, oauth_token="not-an-oauth-token",
    )
    assert invitation is None


def test_token_optional_only_sets_timestamp(db):
    """Passing oauth_token=None marks the receipt without overwriting
    any existing on-disk token reference. This is the path the new
    claude.py login takes when it wants the DB updated but the
    on-disk file is the source of truth."""
    dev_id = TEST_DEV_PREFIX + "timestamp_only"
    _seed_invitation(db, dev_id)

    invitation = record_oauth_token_for_dev(db, dev_id=dev_id, oauth_token=None)
    assert invitation is not None
    assert invitation.oauth_token_received_at is not None
    # oauth_token column stays NULL since we didn't pass one.
    assert invitation.oauth_token is None
