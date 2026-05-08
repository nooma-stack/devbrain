"""Tests for onboard_reconciler.

Covers the post-2026-05-07-redesign branch where invitations land at
status='ready' with a NULL oauth_token (the dev's auth credential
gets generated server-side later via `devbrain login` instead of
transiting the kit's Phase 5). The reconciler must:

  - Append pubkey to authorized_keys
  - Provision profile dir + .gitconfig
  - Mark status='activated'
  - SKIP the credential stash step (no token to stash)
  - NOT roll back authorized_keys on the missing token

Plus the legacy branch (oauth_token present) must still work for any
in-flight kits issued under the old flow.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Defer-import after sys.path tweak to mirror how the script runs
sys.path.insert(0, str(Path(__file__).parent.parent))
from onboard_reconciler import _try_activate  # type: ignore


@pytest.fixture
def fake_invitation():
    """A SimpleNamespace stand-in for the listing-side invitation summary."""
    return SimpleNamespace(
        id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        dev_id="alice",
    )


@pytest.fixture
def authorized_keys(tmp_path: Path) -> Path:
    ak = tmp_path / ".ssh" / "authorized_keys"
    ak.parent.mkdir(parents=True, exist_ok=True)
    ak.write_text("# pre-existing entry\nssh-ed25519 AAAA pre-existing\n")
    ak.chmod(0o600)
    return ak


def _build_db_mock(row_tuple) -> MagicMock:
    """Construct a MagicMock that returns row_tuple from BOTH the lock-
    acquisition cursor AND the row-fetch cursor (the reconciler does
    two SELECTs inside a single _try_activate call)."""
    mock_cur = MagicMock()
    # First call: SELECT pg_try_advisory_xact_lock(...) → True
    # Second call: SELECT id, dev_id, ... → row_tuple
    mock_cur.fetchone.side_effect = [(True,), row_tuple]
    mock_cur.__enter__ = lambda s: s
    mock_cur.__exit__ = MagicMock(return_value=False)

    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__ = MagicMock(return_value=False)

    db = MagicMock()
    db._conn.return_value = mock_conn
    db.get_dev = MagicMock(return_value={"dev_id": "alice", "full_name": "Alice", "email": "alice@example.com"})
    return db


def test_activate_succeeds_with_null_oauth_token(
    fake_invitation, authorized_keys, tmp_path: Path, monkeypatch
):
    """Post-redesign: oauth_token is NULL at activation. Reconciler must
    still append the pubkey and mark activated; just skips the stash step."""
    pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI alice@example.com"
    # row tuple matches the SELECT in _try_activate:
    # (id_, dev_id, pubkey, oauth_token, status, auto_activate, email, notes, cli)
    row = (
        fake_invitation.id, "alice", pubkey, None, "ready", True,
        "alice@example.com", None, "claude",
    )
    db = _build_db_mock(row)

    profile_dir = tmp_path / "profiles" / "alice"
    monkeypatch.setattr(
        "onboard_reconciler._resolve_profile_dir", lambda dev_id: profile_dir
    )
    monkeypatch.setattr(
        "onboard_reconciler._resolve_authorized_keys", lambda: authorized_keys
    )

    with patch("onboard_reconciler._notify_admin"):
        result = _try_activate(db, fake_invitation, default_authorized_keys=authorized_keys)

    assert result is True
    # Pubkey appended to authorized_keys (preserving pre-existing entry)
    ak_text = authorized_keys.read_text()
    assert "pre-existing" in ak_text
    assert pubkey in ak_text
    assert "# devbrain:alice:" in ak_text
    # Profile dir provisioned
    assert profile_dir.exists()
    # No credential file stashed (skipped because oauth_token was NULL)
    assert not (profile_dir / ".claude" / "oauth-token").exists()


def test_activate_succeeds_with_oauth_token_present(
    fake_invitation, authorized_keys, tmp_path: Path, monkeypatch
):
    """Legacy branch: oauth_token is set. Reconciler stashes it AND
    appends the pubkey. Forward-compat check for any in-flight pre-
    redesign kits."""
    pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI alice@example.com"
    legacy_token = "sk-ant-oat01-LEGACY-IN-FLIGHT-TOKEN"
    row = (
        fake_invitation.id, "alice", pubkey, legacy_token, "ready", True,
        "alice@example.com", None, "claude",
    )
    db = _build_db_mock(row)

    profile_dir = tmp_path / "profiles" / "alice"
    monkeypatch.setattr(
        "onboard_reconciler._resolve_profile_dir", lambda dev_id: profile_dir
    )
    monkeypatch.setattr(
        "onboard_reconciler._resolve_authorized_keys", lambda: authorized_keys
    )

    with patch("onboard_reconciler._notify_admin"):
        result = _try_activate(db, fake_invitation, default_authorized_keys=authorized_keys)

    assert result is True
    ak_text = authorized_keys.read_text()
    assert pubkey in ak_text
    # Legacy: token stashed at the per-CLI path
    token_file = profile_dir / ".claude" / "oauth-token"
    assert token_file.exists()
    assert token_file.read_text() == legacy_token


def test_activate_skips_when_status_is_not_ready(
    fake_invitation, authorized_keys, tmp_path: Path, monkeypatch
):
    """If the row state changed to 'activated' between the listing and
    the lock acquisition, the reconciler must not double-process."""
    pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI alice@example.com"
    row = (
        fake_invitation.id, "alice", pubkey, None, "activated", True,
        "alice@example.com", None, "claude",
    )
    db = _build_db_mock(row)

    monkeypatch.setattr(
        "onboard_reconciler._resolve_authorized_keys", lambda: authorized_keys
    )

    result = _try_activate(db, fake_invitation, default_authorized_keys=authorized_keys)

    assert result is False
    # authorized_keys untouched
    assert "pre-existing" in authorized_keys.read_text()
    assert pubkey not in authorized_keys.read_text()


def test_activate_skips_when_auto_activate_false(
    fake_invitation, authorized_keys, tmp_path: Path, monkeypatch
):
    """auto_activate=False means the admin wants a manual gate; the
    reconciler emits an admin notification but doesn't write
    authorized_keys."""
    pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI alice@example.com"
    row = (
        fake_invitation.id, "alice", pubkey, None, "ready", False,
        "alice@example.com", None, "claude",
    )
    db = _build_db_mock(row)

    monkeypatch.setattr(
        "onboard_reconciler._resolve_authorized_keys", lambda: authorized_keys
    )

    with patch("onboard_reconciler._notify_admin") as notify:
        result = _try_activate(db, fake_invitation, default_authorized_keys=authorized_keys)

    assert result is False
    assert pubkey not in authorized_keys.read_text()
    notify.assert_called_once()
    kwargs = notify.call_args.kwargs
    assert kwargs.get("status") == "ready_pending_manual"
