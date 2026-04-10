# DevBrain Notifications System Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a pluggable, provider-agnostic notification system for DevBrain that delivers factory job events through multiple channels (tmux, SMTP, Gmail DWD, Google Chat DWD, Telegram, Slack/Discord/generic webhooks), with per-dev channel preferences, a history CLI with natural language queries, and an MCP tool for AI-driven notifications.

**Architecture:** Channels implement a common `NotificationChannel` protocol and are registered in a channel registry. The `NotificationRouter` iterates each recipient's configured channels and dispatches events through the enabled ones. Per-dev channel addresses are stored in a `channels` JSONB column on the `devs` table — each dev chooses which channels they want and provides the corresponding address (email, chat_id, webhook URL, etc.). All notifications are recorded in the DB for history and audit. A `devbrain` CLI handles dev registration, history queries, and Telegram chat_id auto-discovery. Setup is fully generic — users ship their own config with their own credentials, no hardcoded company values.

**Tech Stack:** Python (channels, router, CLI), psycopg2, click, tmux, smtplib, google-api-python-client, urllib (Telegram/webhooks), ollama (for NL query translation), TypeScript (MCP tool)

---

## Task 1: DB Migration — devs (with channels JSONB) + notifications

**Files:**
- Create: `migrations/005_notifications.sql`

**Step 1: Write the migration**

```sql
-- Migration 005: Notifications system
-- Adds devs table with per-dev channel addresses and notifications audit table.

CREATE TABLE devbrain.devs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dev_id              VARCHAR(255) UNIQUE NOT NULL,
    full_name           VARCHAR(255),
    -- Channels is a list of {type, address, metadata?} objects:
    -- [
    --   {"type": "tmux", "address": "alice"},
    --   {"type": "smtp", "address": "alice@example.com"},
    --   {"type": "telegram_bot", "address": "123456789"},
    --   {"type": "webhook_slack", "address": "https://hooks.slack.com/..."}
    -- ]
    channels            JSONB DEFAULT '[]',
    -- Which event types this dev wants to receive
    event_subscriptions JSONB DEFAULT '["job_ready","job_failed","lock_conflict","unblocked","needs_human"]',
    created_at          TIMESTAMPTZ DEFAULT now(),
    updated_at          TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_devs_dev_id ON devbrain.devs(dev_id);

CREATE TABLE devbrain.notifications (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recipient_dev_id    VARCHAR(255) NOT NULL,
    job_id              UUID REFERENCES devbrain.factory_jobs(id) ON DELETE SET NULL,
    event_type          VARCHAR(50) NOT NULL,
    title               VARCHAR(500) NOT NULL,
    body                TEXT NOT NULL,
    channels_attempted  JSONB DEFAULT '[]',
    channels_delivered  JSONB DEFAULT '[]',
    delivery_errors     JSONB DEFAULT '{}',
    sent_at             TIMESTAMPTZ DEFAULT now(),
    metadata            JSONB DEFAULT '{}'
);

CREATE INDEX idx_notifications_recipient ON devbrain.notifications(recipient_dev_id);
CREATE INDEX idx_notifications_job ON devbrain.notifications(job_id);
CREATE INDEX idx_notifications_sent_at ON devbrain.notifications(sent_at DESC);
CREATE INDEX idx_notifications_event_type ON devbrain.notifications(event_type);
```

**Step 2: Run the migration**

Run: `psql "postgresql://devbrain:devbrain-local@localhost:5433/devbrain" -f migrations/005_notifications.sql`
Expected: CREATE TABLE x2, CREATE INDEX x5 — no errors

**Step 3: Verify**

Run: `psql "postgresql://devbrain:devbrain-local@localhost:5433/devbrain" -c "\d devbrain.devs" -c "\d devbrain.notifications"`
Expected: Both tables shown

**Step 4: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add migrations/005_notifications.sql
git commit -m "chore: add devs (with channels) and notifications tables"
```

---

## Task 2: Generic Config — devbrain.yaml.example

**Files:**
- Create: `config/devbrain.yaml.example`
- Modify: `config/devbrain.yaml` (add notifications section; keep gitignored for real values)
- Modify: `.gitignore` (ensure real config is ignored)

**Step 1: Create the example config**

Create `config/devbrain.yaml.example` with the full DevBrain config including notifications. Use placeholder values only — no company-specific data:

```yaml
# DevBrain Configuration (example template)
# Copy to devbrain.yaml and fill in your own values.

database:
  host: localhost
  port: 5433
  user: devbrain
  password: devbrain-local
  database: devbrain

embedding:
  provider: ollama
  url: http://localhost:11434
  model: snowflake-arctic-embed2
  dims: 1024

summarization:
  provider: ollama
  url: http://localhost:11434
  model: qwen2.5:7b

factory:
  max_concurrent_jobs: 2
  max_fix_loop_retries: 5
  cli_preferences:
    planning: claude
    implementing: claude
    review_arch: claude
    review_security: claude
    fix: claude
  cleanup:
    soft_timer_seconds: 600
    extension_seconds: 300
    hard_ceiling_seconds: 1800
    auto_archive_after_hours: 24
    branch_cleanup: true

notifications:
  # Only these event types fire notifications (others just update DB)
  notify_events:
    - job_ready
    - job_failed
    - lock_conflict
    - unblocked
    - needs_human

  # Channel config — enable only the channels you plan to use.
  # Users register their per-channel addresses via `devbrain register --channel TYPE:ADDRESS`
  channels:
    tmux:
      enabled: true
      popup_width: 70
      popup_height: 20

    smtp:
      enabled: false
      host: smtp.example.com
      port: 587
      use_tls: true
      # Username/password can be set here or via SMTP_USERNAME / SMTP_PASSWORD env vars
      username: ""
      password: ""
      sender_email: "devbrain@example.com"
      sender_display_name: "DevBrain"

    gmail_dwd:
      enabled: false
      # Google service account with domain-wide delegation
      service_account_path: "~/.devbrain/credentials/gmail-sa.json"
      # Workspace user the service account impersonates
      sender_email: "devbrain@your-workspace.com"
      sender_display_name: "DevBrain"

    gchat_dwd:
      enabled: false
      service_account_path: "~/.devbrain/credentials/gchat-sa.json"
      sender_email: "devbrain@your-workspace.com"
      auto_create_dm_space: true

    telegram_bot:
      enabled: false
      # From @BotFather — can also set via TELEGRAM_BOT_TOKEN env var
      bot_token: ""
      bot_username: ""  # Display name, e.g., "devbrain_bot"

    webhook_slack:
      enabled: false
      # Webhook URLs are stored per-dev (in their channels config), not here

    webhook_discord:
      enabled: false

    webhook_generic:
      enabled: false
      # Generic webhook sends JSON: {"title": "...", "body": "...", "event_type": "..."}
```

**Step 2: Add notifications section to real devbrain.yaml**

Append the same `notifications:` section to the existing `config/devbrain.yaml`. This stays local — not checked in if it contains real secrets.

**Step 3: Update .gitignore**

Ensure `.gitignore` includes:
```
# Local config with secrets
config/devbrain.yaml
!config/devbrain.yaml.example

# Credentials
.devbrain/credentials/
~/.devbrain/
```

**Step 4: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add config/devbrain.yaml.example .gitignore
git commit -m "chore: add generic devbrain.yaml.example with notification channel templates"
```

---

## Task 3: Devs + Notifications CRUD (FactoryDB methods)

**Files:**
- Modify: `factory/state_machine.py`
- Test: `factory/tests/test_devs_notifications_crud.py`

**Step 1: Write the failing test**

Create `factory/tests/test_devs_notifications_crud.py`:

```python
"""Tests for devs and notifications CRUD."""
import pytest
from state_machine import FactoryDB

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture
def cleanup_test_data(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM devbrain.notifications WHERE recipient_dev_id LIKE 'test_%'")
        cur.execute("DELETE FROM devbrain.devs WHERE dev_id LIKE 'test_%'")
        conn.commit()


# ─── Devs CRUD ─────────────────────────────────────────────────────────

def test_register_dev_with_channels(db, cleanup_test_data):
    channels = [
        {"type": "tmux", "address": "test_alice"},
        {"type": "smtp", "address": "alice@example.com"},
    ]
    db.register_dev(
        dev_id="test_alice",
        full_name="Alice Test",
        channels=channels,
    )
    dev = db.get_dev("test_alice")
    assert dev["dev_id"] == "test_alice"
    assert len(dev["channels"]) == 2
    assert dev["channels"][0]["type"] == "tmux"


def test_register_dev_upsert(db, cleanup_test_data):
    """Re-registering a dev updates their channels."""
    db.register_dev(
        dev_id="test_upsert",
        channels=[{"type": "tmux", "address": "test_upsert"}],
    )
    db.register_dev(
        dev_id="test_upsert",
        channels=[
            {"type": "tmux", "address": "test_upsert"},
            {"type": "telegram_bot", "address": "12345"},
        ],
    )
    dev = db.get_dev("test_upsert")
    assert len(dev["channels"]) == 2


def test_add_dev_channel(db, cleanup_test_data):
    """Add a single channel to an existing dev without replacing others."""
    db.register_dev(
        dev_id="test_addchan",
        channels=[{"type": "tmux", "address": "test_addchan"}],
    )
    db.add_dev_channel("test_addchan", {"type": "webhook_slack", "address": "https://hooks.slack.com/xyz"})
    dev = db.get_dev("test_addchan")
    assert len(dev["channels"]) == 2
    assert any(c["type"] == "webhook_slack" for c in dev["channels"])


def test_remove_dev_channel(db, cleanup_test_data):
    """Remove a channel by type."""
    db.register_dev(
        dev_id="test_remchan",
        channels=[
            {"type": "tmux", "address": "test_remchan"},
            {"type": "smtp", "address": "foo@example.com"},
        ],
    )
    db.remove_dev_channel("test_remchan", channel_type="smtp")
    dev = db.get_dev("test_remchan")
    assert len(dev["channels"]) == 1
    assert dev["channels"][0]["type"] == "tmux"


def test_get_nonexistent_dev(db):
    assert db.get_dev("test_nobody_xyz_123") is None


def test_list_devs(db, cleanup_test_data):
    db.register_dev(dev_id="test_lista", channels=[])
    db.register_dev(dev_id="test_listb", channels=[])
    devs = db.list_devs()
    ids = [d["dev_id"] for d in devs]
    assert "test_lista" in ids
    assert "test_listb" in ids


# ─── Notifications CRUD ────────────────────────────────────────────────

def test_record_and_get_notification(db, cleanup_test_data):
    notif_id = db.record_notification(
        recipient_dev_id="test_notif_dev",
        event_type="job_ready",
        title="Test ready",
        body="Test body",
        channels_attempted=["tmux", "smtp"],
        channels_delivered=["smtp"],
        delivery_errors={"tmux": "no session"},
    )
    assert notif_id is not None

    notifs = db.get_notifications(recipient_dev_id="test_notif_dev", limit=10)
    assert len(notifs) >= 1
    n = notifs[0]
    assert n["event_type"] == "job_ready"
    assert "smtp" in n["channels_delivered"]
    assert "tmux" in n["delivery_errors"]


def test_get_notifications_filtered(db, cleanup_test_data):
    db.record_notification(
        recipient_dev_id="test_filt",
        event_type="job_failed",
        title="A", body="A",
        channels_attempted=["tmux"], channels_delivered=["tmux"],
    )
    db.record_notification(
        recipient_dev_id="test_filt",
        event_type="job_ready",
        title="B", body="B",
        channels_attempted=["tmux"], channels_delivered=["tmux"],
    )
    failed = db.get_notifications(recipient_dev_id="test_filt", event_type="job_failed", limit=10)
    assert all(n["event_type"] == "job_failed" for n in failed)
```

**Step 2: Run test to verify failure**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_devs_notifications_crud.py -v`
Expected: FAIL — methods don't exist

**Step 3: Implement the methods**

Add to `FactoryDB` class in `factory/state_machine.py` (after the existing `get_cleanup_reports` method):

```python
    # ─── Devs CRUD ────────────────────────────────────────────────────────

    def register_dev(
        self,
        dev_id: str,
        full_name: str | None = None,
        channels: list[dict] | None = None,
        event_subscriptions: list[str] | None = None,
    ) -> str:
        """Register a new dev or update an existing one.

        channels is a list of {type, address, metadata?} dicts.
        event_subscriptions is a list of event_type strings the dev wants to receive.
        """
        chans = channels or []
        events = event_subscriptions or [
            "job_ready", "job_failed", "lock_conflict", "unblocked", "needs_human",
        ]

        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO devbrain.devs (dev_id, full_name, channels, event_subscriptions)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (dev_id) DO UPDATE SET
                    full_name = COALESCE(EXCLUDED.full_name, devbrain.devs.full_name),
                    channels = EXCLUDED.channels,
                    event_subscriptions = EXCLUDED.event_subscriptions,
                    updated_at = now()
                RETURNING id
                """,
                (dev_id, full_name, json.dumps(chans), json.dumps(events)),
            )
            conn.commit()
            return str(cur.fetchone()[0])

    def get_dev(self, dev_id: str) -> dict | None:
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
                "created_at": str(row[5]),
                "updated_at": str(row[6]),
            }

    def list_devs(self) -> list[dict]:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, dev_id, full_name, channels, event_subscriptions
                FROM devbrain.devs
                ORDER BY dev_id
                """
            )
            return [
                {
                    "id": str(r[0]), "dev_id": r[1], "full_name": r[2],
                    "channels": r[3] or [], "event_subscriptions": r[4] or [],
                }
                for r in cur.fetchall()
            ]

    def add_dev_channel(self, dev_id: str, channel: dict) -> None:
        """Add a single channel to a dev, preserving existing channels."""
        dev = self.get_dev(dev_id)
        if not dev:
            raise ValueError(f"Dev {dev_id} not found")
        # Replace any existing channel of the same type+address
        existing = [
            c for c in dev["channels"]
            if not (c.get("type") == channel.get("type") and c.get("address") == channel.get("address"))
        ]
        existing.append(channel)
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE devbrain.devs SET channels = %s, updated_at = now() WHERE dev_id = %s",
                (json.dumps(existing), dev_id),
            )
            conn.commit()

    def remove_dev_channel(self, dev_id: str, channel_type: str, address: str | None = None) -> None:
        """Remove channels matching type (and optionally address)."""
        dev = self.get_dev(dev_id)
        if not dev:
            raise ValueError(f"Dev {dev_id} not found")
        filtered = [
            c for c in dev["channels"]
            if not (c.get("type") == channel_type and (address is None or c.get("address") == address))
        ]
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE devbrain.devs SET channels = %s, updated_at = now() WHERE dev_id = %s",
                (json.dumps(filtered), dev_id),
            )
            conn.commit()

    # ─── Notifications CRUD ───────────────────────────────────────────────

    def record_notification(
        self,
        recipient_dev_id: str,
        event_type: str,
        title: str,
        body: str,
        job_id: str | None = None,
        channels_attempted: list[str] | None = None,
        channels_delivered: list[str] | None = None,
        delivery_errors: dict | None = None,
        metadata: dict | None = None,
    ) -> str:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO devbrain.notifications
                    (recipient_dev_id, job_id, event_type, title, body,
                     channels_attempted, channels_delivered, delivery_errors, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    recipient_dev_id, job_id, event_type, title, body,
                    json.dumps(channels_attempted or []),
                    json.dumps(channels_delivered or []),
                    json.dumps(delivery_errors or {}),
                    json.dumps(metadata or {}),
                ),
            )
            notif_id = str(cur.fetchone()[0])
            conn.commit()
            return notif_id

    def get_notifications(
        self,
        recipient_dev_id: str | None = None,
        job_id: str | None = None,
        event_type: str | None = None,
        since_hours: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        conditions = []
        params: list = []
        if recipient_dev_id:
            conditions.append("recipient_dev_id = %s")
            params.append(recipient_dev_id)
        if job_id:
            conditions.append("job_id = %s")
            params.append(job_id)
        if event_type:
            conditions.append("event_type = %s")
            params.append(event_type)
        if since_hours:
            conditions.append("sent_at > now() - (interval '1 hour' * %s)")
            params.append(since_hours)

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
                    "sent_at": str(r[9]),
                    "metadata": r[10] or {},
                }
                for r in cur.fetchall()
            ]
```

**Step 4: Run tests**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_devs_notifications_crud.py -v`
Expected: All tests PASS

**Step 5: Run full suite**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/state_machine.py factory/tests/test_devs_notifications_crud.py
git commit -m "feat: add devs and notifications CRUD with pluggable channels JSONB"
```

---

## Task 4: NotificationChannel Base Class + Channel Registry

**Files:**
- Create: `factory/notifications/__init__.py`
- Create: `factory/notifications/base.py`
- Test: `factory/tests/test_notification_base.py`

**Step 1: Write the failing test**

Create `factory/tests/test_notification_base.py`:

```python
"""Tests for NotificationChannel base class and registry."""
import pytest
from notifications.base import (
    NotificationChannel,
    ChannelResult,
    ChannelRegistry,
)


class DummyChannel(NotificationChannel):
    name = "dummy"

    def __init__(self, **config):
        self.config = config

    def is_configured(self) -> bool:
        return self.config.get("enabled", False)

    def send(self, address: str, title: str, body: str) -> ChannelResult:
        return ChannelResult(
            delivered=True,
            channel=self.name,
            metadata={"address": address},
        )


def test_channel_result_dataclass():
    r = ChannelResult(delivered=True, channel="dummy")
    assert r.delivered is True
    assert r.channel == "dummy"
    assert r.error is None


def test_dummy_channel_implements_protocol():
    ch = DummyChannel(enabled=True)
    assert ch.name == "dummy"
    assert ch.is_configured() is True
    result = ch.send("test@example.com", "Title", "Body")
    assert result.delivered is True


def test_registry_register_and_get():
    registry = ChannelRegistry()
    registry.register("dummy", DummyChannel)
    ch_class = registry.get("dummy")
    assert ch_class is DummyChannel


def test_registry_get_unknown():
    registry = ChannelRegistry()
    assert registry.get("nonexistent") is None


def test_registry_list_types():
    registry = ChannelRegistry()
    registry.register("dummy", DummyChannel)
    registry.register("dummy2", DummyChannel)
    assert set(registry.list_types()) == {"dummy", "dummy2"}


def test_registry_instantiate():
    registry = ChannelRegistry()
    registry.register("dummy", DummyChannel)
    ch = registry.instantiate("dummy", enabled=True)
    assert isinstance(ch, DummyChannel)
    assert ch.is_configured() is True
```

**Step 2: Run tests to verify failures**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_notification_base.py -v`
Expected: FAIL

**Step 3: Implement base.py**

Create `factory/notifications/__init__.py` (empty).

Create `factory/notifications/base.py`:

```python
"""Base classes for the DevBrain notification channel system.

Channels implement NotificationChannel and are registered in a ChannelRegistry.
The router uses the registry to instantiate channels from config.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Type

logger = logging.getLogger(__name__)


@dataclass
class ChannelResult:
    """Result of a notification delivery attempt."""
    delivered: bool
    channel: str
    error: str | None = None
    metadata: dict = field(default_factory=dict)


class NotificationChannel(ABC):
    """Base class for notification delivery channels.

    Subclasses must:
    - Set a `name` class attribute (unique identifier)
    - Implement is_configured() and send()
    - Accept keyword arguments in __init__ for config values
    """

    name: str = ""

    def __init__(self, **config):
        self.config = config

    @abstractmethod
    def is_configured(self) -> bool:
        """Return True if the channel has enough config to actually send."""
        ...

    @abstractmethod
    def send(self, address: str, title: str, body: str) -> ChannelResult:
        """Deliver a notification to the given address.

        Address format is channel-specific:
          - tmux: session name
          - smtp: email address
          - gmail_dwd: email address
          - gchat_dwd: email address (or existing space ID)
          - telegram_bot: chat_id
          - webhook_*: URL
        """
        ...


class ChannelRegistry:
    """Registry mapping channel type names to channel classes."""

    def __init__(self):
        self._channels: dict[str, Type[NotificationChannel]] = {}

    def register(self, name: str, channel_class: Type[NotificationChannel]) -> None:
        self._channels[name] = channel_class
        logger.debug("Registered channel: %s → %s", name, channel_class.__name__)

    def get(self, name: str) -> Type[NotificationChannel] | None:
        return self._channels.get(name)

    def list_types(self) -> list[str]:
        return list(self._channels.keys())

    def instantiate(self, name: str, **config) -> NotificationChannel | None:
        cls = self.get(name)
        if cls is None:
            return None
        return cls(**config)


# Global registry — channels register themselves on import
default_registry = ChannelRegistry()
```

**Step 4: Run tests**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_notification_base.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/notifications/__init__.py factory/notifications/base.py factory/tests/test_notification_base.py
git commit -m "feat: add NotificationChannel base class and ChannelRegistry"
```

---

## Task 5: Tmux Channel

**Files:**
- Create: `factory/notifications/channels/__init__.py`
- Create: `factory/notifications/channels/tmux.py`
- Test: `factory/tests/test_channel_tmux.py`

**Step 1: Write the failing test**

Create `factory/tests/test_channel_tmux.py`:

```python
"""Tests for tmux notification channel."""
import pytest
import subprocess
from unittest.mock import patch, MagicMock
from notifications.channels.tmux import TmuxChannel


@pytest.fixture
def channel():
    return TmuxChannel(popup_width=70, popup_height=20)


def test_is_configured(channel):
    """tmux channel is configured if tmux binary exists."""
    with patch("shutil.which", return_value="/usr/bin/tmux"):
        assert channel.is_configured() is True
    with patch("shutil.which", return_value=None):
        assert channel.is_configured() is False


def test_send_no_session(channel):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1)  # has-session fails
        result = channel.send("nonexistent", "Title", "Body")
    assert result.delivered is False
    assert "no active session" in result.error.lower()


def test_send_popup_success(channel):
    call_count = {"n": 0}

    def mock_run_side_effect(*args, **kwargs):
        call_count["n"] += 1
        # First call is has-session, second is display-popup
        return MagicMock(returncode=0, stderr=b"")

    with patch("subprocess.run", side_effect=mock_run_side_effect):
        result = channel.send("alice", "Test", "Test body")
    assert result.delivered is True
    assert call_count["n"] >= 2


def test_send_handles_subprocess_error(channel):
    def mock_run_side_effect(*args, **kwargs):
        if "has-session" in args[0]:
            return MagicMock(returncode=0)
        raise subprocess.SubprocessError("tmux crashed")

    with patch("subprocess.run", side_effect=mock_run_side_effect):
        result = channel.send("alice", "Test", "Test")
    assert result.delivered is False
```

**Step 2: Run to verify failure**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_channel_tmux.py -v`
Expected: FAIL

**Step 3: Implement tmux.py**

Create `factory/notifications/channels/__init__.py` (empty).

Create `factory/notifications/channels/tmux.py`:

```python
"""Tmux notification channel — non-disruptive popup overlay."""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from notifications.base import NotificationChannel, ChannelResult, default_registry

logger = logging.getLogger(__name__)


class TmuxChannel(NotificationChannel):
    name = "tmux"

    def __init__(self, popup_width: int = 70, popup_height: int = 20, **kwargs):
        super().__init__(**kwargs)
        self.popup_width = popup_width
        self.popup_height = popup_height

    def is_configured(self) -> bool:
        return shutil.which("tmux") is not None

    def _is_session_active(self, session_name: str) -> bool:
        try:
            result = subprocess.run(
                ["tmux", "has-session", "-t", session_name],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0
        except (subprocess.SubprocessError, FileNotFoundError):
            return False

    def send(self, address: str, title: str, body: str) -> ChannelResult:
        # 'address' is the tmux session name for tmux channel
        session_name = address

        if not self._is_session_active(session_name):
            return ChannelResult(
                delivered=False,
                channel=self.name,
                error=f"No active tmux session '{session_name}'",
            )

        tmp_file = Path(tempfile.gettempdir()) / f"devbrain-notif-{uuid.uuid4().hex[:8]}.txt"

        try:
            content = self._format(title, body)
            tmp_file.write_text(content)

            popup_cmd = (
                f"cat {tmp_file} && echo '' && "
                f"echo '[Press any key to dismiss]' && "
                f"read -n 1 && rm {tmp_file}"
            )

            result = subprocess.run(
                [
                    "tmux", "display-popup",
                    "-t", session_name,
                    "-w", str(self.popup_width),
                    "-h", str(self.popup_height),
                    "-E", popup_cmd,
                ],
                capture_output=True, timeout=10,
            )

            if result.returncode != 0:
                return ChannelResult(
                    delivered=False,
                    channel=self.name,
                    error=f"tmux exit {result.returncode}: {result.stderr.decode()[:200]}",
                )

            logger.info("Tmux popup delivered to session '%s'", session_name)
            return ChannelResult(delivered=True, channel=self.name)

        except Exception as e:
            try:
                tmp_file.unlink(missing_ok=True)
            except Exception:
                pass
            return ChannelResult(
                delivered=False,
                channel=self.name,
                error=f"{type(e).__name__}: {e}",
            )

    def _format(self, title: str, body: str) -> str:
        sep = "=" * 60
        return f"🔔  DevBrain Factory\n{sep}\n\n{title}\n\n{sep}\n\n{body}\n"


# Register with the default registry
default_registry.register("tmux", TmuxChannel)
```

**Step 4: Run tests**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_channel_tmux.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/notifications/channels/ factory/tests/test_channel_tmux.py
git commit -m "feat: add tmux notification channel with display-popup"
```

---

## Task 6: SMTP Channel (with auth)

**Files:**
- Create: `factory/notifications/channels/smtp.py`
- Test: `factory/tests/test_channel_smtp.py`

**Step 1: Write the failing test**

Create `factory/tests/test_channel_smtp.py`:

```python
"""Tests for SMTP notification channel."""
import pytest
import smtplib
from unittest.mock import patch, MagicMock
from notifications.channels.smtp import SmtpChannel


@pytest.fixture
def channel():
    return SmtpChannel(
        host="smtp.example.com",
        port=587,
        use_tls=True,
        username="user",
        password="pass",
        sender_email="devbrain@example.com",
        sender_display_name="DevBrain",
    )


def test_is_configured_complete(channel):
    assert channel.is_configured() is True


def test_is_configured_missing_host():
    ch = SmtpChannel(host="", port=587, sender_email="x@y.com")
    assert ch.is_configured() is False


def test_is_configured_missing_sender():
    ch = SmtpChannel(host="smtp.example.com", port=587, sender_email="")
    assert ch.is_configured() is False


def test_send_success(channel):
    with patch("smtplib.SMTP") as mock_smtp:
        mock_conn = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_conn
        result = channel.send("alice@example.com", "Test", "Body content")
    assert result.delivered is True
    mock_conn.starttls.assert_called_once()
    mock_conn.login.assert_called_once_with("user", "pass")
    mock_conn.send_message.assert_called_once()


def test_send_no_tls():
    ch = SmtpChannel(
        host="smtp.example.com",
        port=25,
        use_tls=False,
        sender_email="devbrain@example.com",
    )
    with patch("smtplib.SMTP") as mock_smtp:
        mock_conn = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_conn
        result = ch.send("alice@example.com", "Test", "Body")
    assert result.delivered is True
    mock_conn.starttls.assert_not_called()


def test_send_auth_failure(channel):
    with patch("smtplib.SMTP") as mock_smtp:
        mock_conn = MagicMock()
        mock_smtp.return_value.__enter__.return_value = mock_conn
        mock_conn.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Auth failed")
        result = channel.send("alice@example.com", "Test", "Body")
    assert result.delivered is False
    assert "auth" in result.error.lower()


def test_env_var_credentials():
    """Credentials can come from env vars if not in config."""
    with patch.dict("os.environ", {"SMTP_USERNAME": "envuser", "SMTP_PASSWORD": "envpass"}):
        ch = SmtpChannel(
            host="smtp.example.com",
            port=587,
            sender_email="devbrain@example.com",
        )
        assert ch._username() == "envuser"
        assert ch._password() == "envpass"
```

**Step 2: Run to verify failure**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_channel_smtp.py -v`
Expected: FAIL

**Step 3: Implement smtp.py**

Create `factory/notifications/channels/smtp.py`:

```python
"""SMTP notification channel with authentication support.

Works with any SMTP server: Gmail (with app password), Fastmail, Mailgun, SES, etc.
Credentials can be set in config or via SMTP_USERNAME / SMTP_PASSWORD env vars.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from notifications.base import NotificationChannel, ChannelResult, default_registry

logger = logging.getLogger(__name__)


class SmtpChannel(NotificationChannel):
    name = "smtp"

    def __init__(
        self,
        host: str = "",
        port: int = 587,
        use_tls: bool = True,
        username: str = "",
        password: str = "",
        sender_email: str = "",
        sender_display_name: str = "DevBrain",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self._config_username = username
        self._config_password = password
        self.sender_email = sender_email
        self.sender_display_name = sender_display_name

    def _username(self) -> str:
        return self._config_username or os.environ.get("SMTP_USERNAME", "")

    def _password(self) -> str:
        return self._config_password or os.environ.get("SMTP_PASSWORD", "")

    def is_configured(self) -> bool:
        return bool(self.host and self.sender_email)

    def send(self, address: str, title: str, body: str) -> ChannelResult:
        if not self.is_configured():
            return ChannelResult(
                delivered=False,
                channel=self.name,
                error="SMTP channel not configured (host/sender_email required)",
            )

        try:
            msg = MIMEMultipart()
            msg["From"] = f"{self.sender_display_name} <{self.sender_email}>"
            msg["To"] = address
            msg["Subject"] = title
            msg.attach(MIMEText(body, "plain"))

            with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
                if self.use_tls:
                    smtp.starttls()
                if self._username():
                    smtp.login(self._username(), self._password())
                smtp.send_message(msg)

            logger.info("SMTP delivered to %s", address)
            return ChannelResult(delivered=True, channel=self.name)

        except smtplib.SMTPAuthenticationError as e:
            return ChannelResult(
                delivered=False,
                channel=self.name,
                error=f"SMTP auth failed: {e}",
            )
        except Exception as e:
            return ChannelResult(
                delivered=False,
                channel=self.name,
                error=f"SMTP {type(e).__name__}: {e}",
            )


default_registry.register("smtp", SmtpChannel)
```

**Step 4: Run tests**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_channel_smtp.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/notifications/channels/smtp.py factory/tests/test_channel_smtp.py
git commit -m "feat: add SMTP notification channel with auth and env var credentials"
```

---

## Task 7: Gmail DWD Channel

**Files:**
- Create: `factory/notifications/channels/gmail_dwd.py`
- Test: `factory/tests/test_channel_gmail_dwd.py`
- Modify: `requirements.txt` (add `google-api-python-client`, `google-auth`)

**Step 1: Install dependencies**

Run: `cd /Users/patrickkelly/devbrain && .venv/bin/pip install google-api-python-client google-auth`
Expected: Successfully installed

**Step 2: Write the failing test**

Create `factory/tests/test_channel_gmail_dwd.py`:

```python
"""Tests for Gmail DWD notification channel."""
import pytest
from unittest.mock import patch, MagicMock
from notifications.channels.gmail_dwd import GmailDwdChannel


@pytest.fixture
def channel():
    return GmailDwdChannel(
        service_account_path="/tmp/fake-sa.json",
        sender_email="devbrain@example.com",
        sender_display_name="DevBrain",
    )


def test_is_configured_missing_file(channel):
    with patch("pathlib.Path.exists", return_value=False):
        assert channel.is_configured() is False


def test_is_configured_complete(channel):
    with patch("pathlib.Path.exists", return_value=True):
        assert channel.is_configured() is True


def test_send_success(channel):
    with patch("pathlib.Path.exists", return_value=True):
        with patch("notifications.channels.gmail_dwd.service_account"):
            with patch("notifications.channels.gmail_dwd.build") as mock_build:
                mock_service = MagicMock()
                mock_build.return_value = mock_service
                mock_service.users().messages().send().execute.return_value = {"id": "msg123"}

                result = channel.send("alice@example.com", "Job Ready", "Body")

    assert result.delivered is True
    assert result.metadata.get("message_id") == "msg123"


def test_send_api_error(channel):
    with patch("pathlib.Path.exists", return_value=True):
        with patch("notifications.channels.gmail_dwd.service_account"):
            with patch("notifications.channels.gmail_dwd.build") as mock_build:
                mock_service = MagicMock()
                mock_build.return_value = mock_service
                mock_service.users().messages().send().execute.side_effect = Exception("quota")
                result = channel.send("alice@example.com", "Test", "Test")
    assert result.delivered is False
    assert "quota" in result.error.lower()
```

**Step 3: Implement gmail_dwd.py**

Create `factory/notifications/channels/gmail_dwd.py`:

```python
"""Gmail notification channel via Google service account with domain-wide delegation.

Requires:
  - GCP service account with DWD enabled
  - Scope: https://www.googleapis.com/auth/gmail.send
  - sender_email must be a Google Workspace user in your domain
"""

from __future__ import annotations

import base64
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from notifications.base import NotificationChannel, ChannelResult, default_registry

logger = logging.getLogger(__name__)

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    GOOGLE_AVAILABLE = True
except ImportError:
    GOOGLE_AVAILABLE = False


SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


class GmailDwdChannel(NotificationChannel):
    name = "gmail_dwd"

    def __init__(
        self,
        service_account_path: str = "",
        sender_email: str = "",
        sender_display_name: str = "DevBrain",
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.service_account_path = Path(service_account_path).expanduser() if service_account_path else None
        self.sender_email = sender_email
        self.sender_display_name = sender_display_name
        self._service = None

    def is_configured(self) -> bool:
        if not GOOGLE_AVAILABLE:
            return False
        if not self.service_account_path or not self.sender_email:
            return False
        return self.service_account_path.exists()

    def _get_service(self):
        if self._service is not None:
            return self._service
        credentials = service_account.Credentials.from_service_account_file(
            str(self.service_account_path), scopes=SCOPES,
        )
        delegated = credentials.with_subject(self.sender_email)
        self._service = build("gmail", "v1", credentials=delegated)
        return self._service

    def send(self, address: str, title: str, body: str) -> ChannelResult:
        if not self.is_configured():
            return ChannelResult(
                delivered=False,
                channel=self.name,
                error="Gmail DWD channel not configured",
            )

        try:
            service = self._get_service()
            msg = MIMEMultipart()
            msg["to"] = address
            msg["from"] = f"{self.sender_display_name} <{self.sender_email}>"
            msg["subject"] = title
            msg.attach(MIMEText(body, "plain"))

            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            send_result = service.users().messages().send(
                userId="me", body={"raw": raw},
            ).execute()

            logger.info("Gmail DWD delivered to %s", address)
            return ChannelResult(
                delivered=True,
                channel=self.name,
                metadata={"message_id": send_result.get("id")},
            )
        except Exception as e:
            return ChannelResult(
                delivered=False,
                channel=self.name,
                error=f"Gmail API: {e}",
            )


default_registry.register("gmail_dwd", GmailDwdChannel)
```

**Step 4: Run tests**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_channel_gmail_dwd.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/notifications/channels/gmail_dwd.py factory/tests/test_channel_gmail_dwd.py requirements.txt
git commit -m "feat: add Gmail DWD notification channel"
```

---

## Task 8: Google Chat DWD Channel

**Files:**
- Create: `factory/notifications/channels/gchat_dwd.py`
- Test: `factory/tests/test_channel_gchat_dwd.py`

**Step 1: Write the failing test**

Create `factory/tests/test_channel_gchat_dwd.py`:

```python
"""Tests for Google Chat DWD notification channel."""
import pytest
from unittest.mock import patch, MagicMock
from notifications.channels.gchat_dwd import GchatDwdChannel


@pytest.fixture
def channel():
    return GchatDwdChannel(
        service_account_path="/tmp/fake-sa.json",
        sender_email="devbrain@example.com",
        auto_create_dm_space=True,
    )


def test_is_configured_missing_file(channel):
    with patch("pathlib.Path.exists", return_value=False):
        assert channel.is_configured() is False


def test_is_configured_complete(channel):
    with patch("pathlib.Path.exists", return_value=True):
        assert channel.is_configured() is True


def test_send_to_existing_space(channel):
    with patch("pathlib.Path.exists", return_value=True):
        with patch("notifications.channels.gchat_dwd.service_account"):
            with patch("notifications.channels.gchat_dwd.build") as mock_build:
                mock_service = MagicMock()
                mock_build.return_value = mock_service
                mock_service.spaces().messages().create().execute.return_value = {
                    "name": "spaces/ABC/messages/1",
                }
                # Use a space ID directly
                result = channel.send("spaces/ABC", "Title", "Body")
    assert result.delivered is True


def test_send_to_email_auto_creates_dm(channel):
    with patch("pathlib.Path.exists", return_value=True):
        with patch("notifications.channels.gchat_dwd.service_account"):
            with patch("notifications.channels.gchat_dwd.build") as mock_build:
                mock_service = MagicMock()
                mock_build.return_value = mock_service
                mock_service.spaces().setup().execute.return_value = {"name": "spaces/NEW"}
                mock_service.spaces().messages().create().execute.return_value = {
                    "name": "spaces/NEW/messages/1",
                }
                result = channel.send("alice@example.com", "Title", "Body")
    assert result.delivered is True


def test_send_api_error(channel):
    with patch("pathlib.Path.exists", return_value=True):
        with patch("notifications.channels.gchat_dwd.service_account"):
            with patch("notifications.channels.gchat_dwd.build") as mock_build:
                mock_service = MagicMock()
                mock_build.return_value = mock_service
                mock_service.spaces().messages().create().execute.side_effect = Exception("permission denied")
                result = channel.send("spaces/ABC", "Title", "Body")
    assert result.delivered is False
    assert "permission" in result.error.lower()
```

**Step 2: Implement gchat_dwd.py**

Create `factory/notifications/channels/gchat_dwd.py`:

```python
"""Google Chat DM notification channel via service account with DWD.

Sends direct messages to users. If the address looks like an email,
auto-creates (or looks up) a DM space with that user on first use.
"""

from __future__ import annotations

import logging
from pathlib import Path

from notifications.base import NotificationChannel, ChannelResult, default_registry

logger = logging.getLogger(__name__)

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    GOOGLE_AVAILABLE = True
except ImportError:
    GOOGLE_AVAILABLE = False


SCOPES = [
    "https://www.googleapis.com/auth/chat.messages.create",
    "https://www.googleapis.com/auth/chat.spaces.create",
]


class GchatDwdChannel(NotificationChannel):
    name = "gchat_dwd"

    def __init__(
        self,
        service_account_path: str = "",
        sender_email: str = "",
        auto_create_dm_space: bool = True,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.service_account_path = Path(service_account_path).expanduser() if service_account_path else None
        self.sender_email = sender_email
        self.auto_create_dm_space = auto_create_dm_space
        self._service = None
        self._email_to_space: dict[str, str] = {}  # Cache

    def is_configured(self) -> bool:
        if not GOOGLE_AVAILABLE:
            return False
        if not self.service_account_path or not self.sender_email:
            return False
        return self.service_account_path.exists()

    def _get_service(self):
        if self._service is not None:
            return self._service
        credentials = service_account.Credentials.from_service_account_file(
            str(self.service_account_path), scopes=SCOPES,
        )
        delegated = credentials.with_subject(self.sender_email)
        self._service = build("chat", "v1", credentials=delegated)
        return self._service

    def _create_dm_space(self, recipient_email: str) -> str | None:
        try:
            service = self._get_service()
            response = service.spaces().setup(
                body={
                    "space": {"spaceType": "DIRECT_MESSAGE"},
                    "memberships": [{
                        "member": {"name": f"users/{recipient_email}", "type": "HUMAN"}
                    }],
                }
            ).execute()
            return response.get("name")
        except Exception as e:
            logger.warning("Failed to create DM space for %s: %s", recipient_email, e)
            return None

    def send(self, address: str, title: str, body: str) -> ChannelResult:
        if not self.is_configured():
            return ChannelResult(
                delivered=False,
                channel=self.name,
                error="Google Chat DWD channel not configured",
            )

        # Resolve address to space_id
        if address.startswith("spaces/"):
            space_id = address
        elif "@" in address and self.auto_create_dm_space:
            if address in self._email_to_space:
                space_id = self._email_to_space[address]
            else:
                space_id = self._create_dm_space(address)
                if space_id:
                    self._email_to_space[address] = space_id
                else:
                    return ChannelResult(
                        delivered=False,
                        channel=self.name,
                        error=f"Could not create DM space for {address}",
                    )
        else:
            return ChannelResult(
                delivered=False,
                channel=self.name,
                error=f"Invalid address format: {address}",
            )

        try:
            service = self._get_service()
            formatted = f"*🔔 {title}*\n\n{body}"
            response = service.spaces().messages().create(
                parent=space_id,
                body={"text": formatted},
            ).execute()
            logger.info("Google Chat delivered to %s", space_id)
            return ChannelResult(
                delivered=True,
                channel=self.name,
                metadata={"space_id": space_id, "message_id": response.get("name")},
            )
        except Exception as e:
            return ChannelResult(
                delivered=False,
                channel=self.name,
                error=f"Chat API: {e}",
            )


default_registry.register("gchat_dwd", GchatDwdChannel)
```

**Step 3: Run tests and commit**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_channel_gchat_dwd.py -v`

```bash
cd /Users/patrickkelly/devbrain
git add factory/notifications/channels/gchat_dwd.py factory/tests/test_channel_gchat_dwd.py
git commit -m "feat: add Google Chat DWD notification channel with auto DM space creation"
```

---

## Task 9: Telegram Bot Channel (with auto-discover helper)

**Files:**
- Create: `factory/notifications/channels/telegram_bot.py`
- Test: `factory/tests/test_channel_telegram.py`

**Step 1: Write the failing test**

Create `factory/tests/test_channel_telegram.py`:

```python
"""Tests for Telegram bot notification channel."""
import json
import pytest
from unittest.mock import patch, MagicMock
from notifications.channels.telegram_bot import TelegramBotChannel


@pytest.fixture
def channel():
    return TelegramBotChannel(bot_token="123:FAKE_TOKEN")


def test_is_configured_with_token(channel):
    assert channel.is_configured() is True


def test_is_configured_no_token():
    ch = TelegramBotChannel(bot_token="")
    assert ch.is_configured() is False


def test_send_success(channel):
    with patch("urllib.request.urlopen") as mock_urlopen:
        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps({"ok": True, "result": {"message_id": 42}}).encode()
        fake_response.__enter__ = lambda self: self
        fake_response.__exit__ = lambda *args: None
        mock_urlopen.return_value = fake_response

        result = channel.send("123456789", "Title", "Body content")

    assert result.delivered is True
    assert result.metadata.get("message_id") == 42


def test_send_api_error(channel):
    with patch("urllib.request.urlopen") as mock_urlopen:
        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps({
            "ok": False,
            "description": "chat not found",
        }).encode()
        fake_response.__enter__ = lambda self: self
        fake_response.__exit__ = lambda *args: None
        mock_urlopen.return_value = fake_response

        result = channel.send("123", "Title", "Body")

    assert result.delivered is False
    assert "chat not found" in result.error.lower()


def test_discover_chat_id(channel):
    """discover_chat_id polls getUpdates and returns the most recent user chat."""
    fake_updates = {
        "ok": True,
        "result": [
            {
                "update_id": 1,
                "message": {
                    "chat": {"id": 987654321, "type": "private", "username": "alice_tg"},
                    "text": "register",
                },
            },
        ],
    }
    with patch("urllib.request.urlopen") as mock_urlopen:
        fake_response = MagicMock()
        fake_response.read.return_value = json.dumps(fake_updates).encode()
        fake_response.__enter__ = lambda self: self
        fake_response.__exit__ = lambda *args: None
        mock_urlopen.return_value = fake_response

        chat_id = channel.discover_chat_id(username_hint="alice_tg")
    assert chat_id == "987654321"
```

**Step 2: Implement telegram_bot.py**

Create `factory/notifications/channels/telegram_bot.py`:

```python
"""Telegram bot notification channel.

Setup:
1. Create a bot via @BotFather, get the token
2. Set bot_token in devbrain.yaml (or TELEGRAM_BOT_TOKEN env var)
3. Each dev DMs the bot once to start a chat
4. Use `devbrain telegram-discover` to get their chat_id automatically
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request

from notifications.base import NotificationChannel, ChannelResult, default_registry

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramBotChannel(NotificationChannel):
    name = "telegram_bot"

    def __init__(self, bot_token: str = "", bot_username: str = "", **kwargs):
        super().__init__(**kwargs)
        self._config_token = bot_token
        self.bot_username = bot_username

    def _token(self) -> str:
        return self._config_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")

    def is_configured(self) -> bool:
        return bool(self._token())

    def _call(self, method: str, params: dict) -> dict:
        url = TELEGRAM_API.format(token=self._token(), method=method)
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    def send(self, address: str, title: str, body: str) -> ChannelResult:
        if not self.is_configured():
            return ChannelResult(
                delivered=False,
                channel=self.name,
                error="Telegram bot token not set",
            )

        text = f"🔔 *{title}*\n\n{body}"

        try:
            response = self._call("sendMessage", {
                "chat_id": address,
                "text": text,
                "parse_mode": "Markdown",
            })

            if not response.get("ok"):
                return ChannelResult(
                    delivered=False,
                    channel=self.name,
                    error=response.get("description", "unknown error"),
                )

            message_id = response.get("result", {}).get("message_id")
            logger.info("Telegram delivered to chat_id %s", address)
            return ChannelResult(
                delivered=True,
                channel=self.name,
                metadata={"message_id": message_id},
            )
        except Exception as e:
            return ChannelResult(
                delivered=False,
                channel=self.name,
                error=f"Telegram API: {e}",
            )

    def discover_chat_id(self, username_hint: str | None = None) -> str | None:
        """Poll getUpdates and return the most recent user chat_id.

        If username_hint is provided, filter updates for that username first.
        """
        if not self.is_configured():
            return None
        try:
            response = self._call("getUpdates", {"limit": 20})
            if not response.get("ok"):
                return None

            updates = response.get("result", [])
            candidates = []
            for update in updates:
                msg = update.get("message") or update.get("edited_message") or {}
                chat = msg.get("chat") or {}
                if chat.get("type") == "private":
                    candidates.append(chat)

            if not candidates:
                return None

            # If a username hint is provided, filter for it
            if username_hint:
                matching = [c for c in candidates if c.get("username") == username_hint]
                if matching:
                    return str(matching[-1]["id"])

            # Return most recent private chat
            return str(candidates[-1]["id"])
        except Exception as e:
            logger.warning("Telegram discover failed: %s", e)
            return None


default_registry.register("telegram_bot", TelegramBotChannel)
```

**Step 3: Run tests and commit**

```bash
cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_channel_telegram.py -v
cd /Users/patrickkelly/devbrain
git add factory/notifications/channels/telegram_bot.py factory/tests/test_channel_telegram.py
git commit -m "feat: add Telegram bot notification channel with chat_id auto-discovery"
```

---

## Task 10: Webhook Channels (Slack, Discord, Generic)

**Files:**
- Create: `factory/notifications/channels/webhook_slack.py`
- Create: `factory/notifications/channels/webhook_discord.py`
- Create: `factory/notifications/channels/webhook_generic.py`
- Create: `factory/notifications/channels/_webhook_base.py`
- Test: `factory/tests/test_channel_webhooks.py`

**Step 1: Write the failing test**

Create `factory/tests/test_channel_webhooks.py`:

```python
"""Tests for webhook notification channels (Slack, Discord, generic)."""
import json
import pytest
from unittest.mock import patch, MagicMock
from notifications.channels.webhook_slack import WebhookSlackChannel
from notifications.channels.webhook_discord import WebhookDiscordChannel
from notifications.channels.webhook_generic import WebhookGenericChannel


def _mock_urlopen_ok():
    fake_response = MagicMock()
    fake_response.read.return_value = b"ok"
    fake_response.getcode.return_value = 200
    fake_response.__enter__ = lambda self: self
    fake_response.__exit__ = lambda *args: None
    return fake_response


# ─── Slack ─────────────────────────────────────────────────────────────

def test_slack_sends_text_field():
    ch = WebhookSlackChannel()
    assert ch.is_configured() is True  # No config needed (URL per dev)

    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_urlopen_ok()
        result = ch.send("https://hooks.slack.com/test", "Title", "Body")

        # Inspect the request body
        call = mock_urlopen.call_args
        req = call[0][0]
        payload = json.loads(req.data.decode())
        assert "text" in payload
        assert "Title" in payload["text"]

    assert result.delivered is True


# ─── Discord ───────────────────────────────────────────────────────────

def test_discord_sends_content_field():
    ch = WebhookDiscordChannel()
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_urlopen_ok()
        result = ch.send("https://discord.com/api/webhooks/test", "Title", "Body")
        call = mock_urlopen.call_args
        req = call[0][0]
        payload = json.loads(req.data.decode())
        assert "content" in payload

    assert result.delivered is True


# ─── Generic ───────────────────────────────────────────────────────────

def test_generic_sends_title_body_fields():
    ch = WebhookGenericChannel()
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _mock_urlopen_ok()
        result = ch.send("https://custom.example.com/hook", "Title", "Body", event_type="job_ready")
        call = mock_urlopen.call_args
        req = call[0][0]
        payload = json.loads(req.data.decode())
        assert payload["title"] == "Title"
        assert payload["body"] == "Body"

    assert result.delivered is True


# ─── Error handling ────────────────────────────────────────────────────

def test_webhook_handles_http_error():
    ch = WebhookSlackChannel()
    with patch("urllib.request.urlopen") as mock_urlopen:
        import urllib.error
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://hooks.slack.com/test", 400, "Bad Request", {}, None,
        )
        result = ch.send("https://hooks.slack.com/test", "Title", "Body")

    assert result.delivered is False
    assert "400" in result.error
```

**Step 2: Implement webhook channels**

Create `factory/notifications/channels/_webhook_base.py`:

```python
"""Shared HTTP POST helper for webhook channels."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from notifications.base import ChannelResult

logger = logging.getLogger(__name__)


def post_webhook(url: str, payload: dict, channel_name: str, timeout: int = 15) -> ChannelResult:
    """POST JSON payload to webhook URL. Returns a ChannelResult."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            if 200 <= code < 300:
                logger.info("Webhook %s delivered to %s (HTTP %s)", channel_name, url[:50], code)
                return ChannelResult(delivered=True, channel=channel_name)
            return ChannelResult(
                delivered=False,
                channel=channel_name,
                error=f"HTTP {code}",
            )
    except urllib.error.HTTPError as e:
        return ChannelResult(
            delivered=False,
            channel=channel_name,
            error=f"HTTP {e.code}: {e.reason}",
        )
    except Exception as e:
        return ChannelResult(
            delivered=False,
            channel=channel_name,
            error=f"{type(e).__name__}: {e}",
        )
```

Create `factory/notifications/channels/webhook_slack.py`:

```python
"""Slack incoming webhook notification channel."""

from __future__ import annotations

from notifications.base import NotificationChannel, ChannelResult, default_registry
from notifications.channels._webhook_base import post_webhook


class WebhookSlackChannel(NotificationChannel):
    name = "webhook_slack"

    def is_configured(self) -> bool:
        # Webhook URLs are stored per-dev, no global config needed
        return True

    def send(self, address: str, title: str, body: str, **kwargs) -> ChannelResult:
        # Slack webhooks expect: {"text": "..."}
        text = f"🔔 *{title}*\n\n{body}"
        return post_webhook(address, {"text": text}, self.name)


default_registry.register("webhook_slack", WebhookSlackChannel)
```

Create `factory/notifications/channels/webhook_discord.py`:

```python
"""Discord webhook notification channel."""

from __future__ import annotations

from notifications.base import NotificationChannel, ChannelResult, default_registry
from notifications.channels._webhook_base import post_webhook


class WebhookDiscordChannel(NotificationChannel):
    name = "webhook_discord"

    def is_configured(self) -> bool:
        return True

    def send(self, address: str, title: str, body: str, **kwargs) -> ChannelResult:
        # Discord webhooks expect: {"content": "..."}
        content = f"🔔 **{title}**\n\n{body}"
        return post_webhook(address, {"content": content}, self.name)


default_registry.register("webhook_discord", WebhookDiscordChannel)
```

Create `factory/notifications/channels/webhook_generic.py`:

```python
"""Generic webhook notification channel.

Sends JSON: {"title": "...", "body": "...", "event_type": "...", "timestamp": "..."}
Works with any HTTP endpoint that accepts JSON POSTs: ntfy.sh, Teams, homelab, etc.
"""

from __future__ import annotations

from datetime import datetime, timezone

from notifications.base import NotificationChannel, ChannelResult, default_registry
from notifications.channels._webhook_base import post_webhook


class WebhookGenericChannel(NotificationChannel):
    name = "webhook_generic"

    def is_configured(self) -> bool:
        return True

    def send(self, address: str, title: str, body: str, event_type: str = "", **kwargs) -> ChannelResult:
        payload = {
            "title": title,
            "body": body,
            "event_type": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return post_webhook(address, payload, self.name)


default_registry.register("webhook_generic", WebhookGenericChannel)
```

**Step 3: Run tests**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_channel_webhooks.py -v`
Expected: All tests PASS

**Step 4: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/notifications/channels/_webhook_base.py \
        factory/notifications/channels/webhook_slack.py \
        factory/notifications/channels/webhook_discord.py \
        factory/notifications/channels/webhook_generic.py \
        factory/tests/test_channel_webhooks.py
git commit -m "feat: add Slack, Discord, and generic webhook notification channels"
```

---

## Task 11: NotificationRouter — Fan-Out with Per-Dev Channels

**Files:**
- Create: `factory/notifications/router.py`
- Test: `factory/tests/test_notification_router.py`

**Step 1: Write the failing test**

Create `factory/tests/test_notification_router.py`:

```python
"""Tests for NotificationRouter."""
import pytest
from unittest.mock import patch
from state_machine import FactoryDB
from notifications.router import NotificationRouter, NotificationEvent

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"


@pytest.fixture
def db():
    return FactoryDB(DATABASE_URL)


@pytest.fixture
def cleanup(db):
    yield
    with db._conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM devbrain.notifications WHERE recipient_dev_id LIKE 'test_%'")
        cur.execute("DELETE FROM devbrain.devs WHERE dev_id LIKE 'test_%'")
        conn.commit()


@pytest.fixture
def router(db):
    return NotificationRouter(db, config={
        "notify_events": ["job_ready", "job_failed", "lock_conflict"],
        "channels": {
            "tmux": {"enabled": True},
            "smtp": {"enabled": False},
        },
    })


def test_router_skips_unsubscribed_events(router, db, cleanup):
    db.register_dev(
        dev_id="test_skipper",
        channels=[{"type": "tmux", "address": "test_skipper"}],
    )
    event = NotificationEvent(
        event_type="phase_transition",  # Not in notify_events
        recipient_dev_id="test_skipper",
        title="Phase",
        body="body",
    )
    result = router.send(event)
    assert result.skipped is True


def test_router_records_notification(router, db, cleanup):
    db.register_dev(
        dev_id="test_recorder",
        channels=[{"type": "tmux", "address": "test_recorder"}],
    )
    event = NotificationEvent(
        event_type="job_ready",
        recipient_dev_id="test_recorder",
        title="Test ready",
        body="body",
    )
    with patch("notifications.channels.tmux.TmuxChannel._is_session_active", return_value=False):
        router.send(event)

    notifs = db.get_notifications(recipient_dev_id="test_recorder", limit=5)
    assert len(notifs) >= 1
    assert notifs[0]["title"] == "Test ready"


def test_router_iterates_dev_channels(router, db, cleanup):
    db.register_dev(
        dev_id="test_multichan",
        channels=[
            {"type": "tmux", "address": "test_multichan"},
            {"type": "smtp", "address": "foo@example.com"},  # Disabled globally
        ],
    )
    event = NotificationEvent(
        event_type="job_failed",
        recipient_dev_id="test_multichan",
        title="Failed",
        body="body",
    )
    with patch("notifications.channels.tmux.TmuxChannel._is_session_active", return_value=False):
        result = router.send(event)

    # Only tmux should be attempted (smtp disabled in router config)
    assert "tmux" in result.channels_attempted
    assert "smtp" not in result.channels_attempted


def test_router_lock_conflict_notifies_both_devs(db, cleanup):
    router = NotificationRouter(db, config={
        "notify_events": ["lock_conflict"],
        "channels": {"tmux": {"enabled": True}},
    })
    db.register_dev(
        dev_id="test_blocked",
        channels=[{"type": "tmux", "address": "test_blocked"}],
    )
    db.register_dev(
        dev_id="test_blocker",
        channels=[{"type": "tmux", "address": "test_blocker"}],
    )

    event = NotificationEvent(
        event_type="lock_conflict",
        recipient_dev_id="test_blocked",
        title="File conflict",
        body="Blocked by blocker",
        metadata={"blocking_dev_id": "test_blocker"},
    )

    with patch("notifications.channels.tmux.TmuxChannel._is_session_active", return_value=False):
        router.send_multi(event)

    blocked = db.get_notifications(recipient_dev_id="test_blocked", limit=5)
    blocker = db.get_notifications(recipient_dev_id="test_blocker", limit=5)
    assert len(blocked) >= 1
    assert len(blocker) >= 1
```

**Step 2: Implement router.py**

Create `factory/notifications/router.py`:

```python
"""Notification router.

Fans notification events out to the recipient's configured channels.
Channels are instantiated from config via the default_registry, and each
dev specifies which channels they want via `devbrain.devs.channels`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from state_machine import FactoryDB
from notifications.base import default_registry, NotificationChannel

# Import all channels so they register with the registry
import notifications.channels.tmux  # noqa: F401
import notifications.channels.smtp  # noqa: F401
import notifications.channels.gmail_dwd  # noqa: F401
import notifications.channels.gchat_dwd  # noqa: F401
import notifications.channels.telegram_bot  # noqa: F401
import notifications.channels.webhook_slack  # noqa: F401
import notifications.channels.webhook_discord  # noqa: F401
import notifications.channels.webhook_generic  # noqa: F401

logger = logging.getLogger(__name__)


@dataclass
class NotificationEvent:
    event_type: str
    recipient_dev_id: str
    title: str
    body: str
    job_id: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class RouterResult:
    skipped: bool = False
    channels_attempted: list[str] = field(default_factory=list)
    channels_delivered: list[str] = field(default_factory=list)
    errors: dict = field(default_factory=dict)


class NotificationRouter:
    def __init__(self, db: FactoryDB, config: dict | None = None):
        self.db = db
        self.config = config or self._load_config()
        # Cache of instantiated channel objects keyed by channel type
        self._channel_cache: dict[str, NotificationChannel] = {}

    @staticmethod
    def _load_config() -> dict:
        config_path = Path(__file__).parent.parent.parent / "config" / "devbrain.yaml"
        if not config_path.exists():
            return {}
        with open(config_path) as f:
            full_config = yaml.safe_load(f) or {}
        return full_config.get("notifications", {})

    def _get_channel(self, channel_type: str) -> NotificationChannel | None:
        """Get (and cache) a channel instance for the given type."""
        if channel_type in self._channel_cache:
            return self._channel_cache[channel_type]

        channel_config = self.config.get("channels", {}).get(channel_type, {})
        if not channel_config.get("enabled", False):
            return None

        instance = default_registry.instantiate(channel_type, **channel_config)
        if instance is None:
            logger.warning("Unknown channel type: %s", channel_type)
            return None

        if not instance.is_configured():
            logger.debug("Channel %s is enabled but not configured", channel_type)
            return None

        self._channel_cache[channel_type] = instance
        return instance

    def send(self, event: NotificationEvent) -> RouterResult:
        """Send a notification event to a single recipient."""
        # Filter event types
        notify_events = self.config.get("notify_events", [])
        if notify_events and event.event_type not in notify_events:
            return RouterResult(skipped=True)

        dev = self.db.get_dev(event.recipient_dev_id)
        if not dev:
            logger.warning("Dev %s not registered", event.recipient_dev_id)
            self.db.record_notification(
                recipient_dev_id=event.recipient_dev_id,
                event_type=event.event_type,
                title=event.title, body=event.body,
                job_id=event.job_id,
                delivery_errors={"router": "dev not registered"},
                metadata=event.metadata,
            )
            return RouterResult()

        # Check dev's event subscriptions
        dev_events = dev.get("event_subscriptions") or []
        if dev_events and event.event_type not in dev_events:
            return RouterResult(skipped=True)

        attempted: list[str] = []
        delivered: list[str] = []
        errors: dict = {}

        # Iterate the dev's registered channels
        for dev_channel in dev.get("channels", []):
            channel_type = dev_channel.get("type")
            address = dev_channel.get("address")
            if not channel_type or not address:
                continue

            channel = self._get_channel(channel_type)
            if channel is None:
                continue

            attempted.append(channel_type)
            try:
                # Pass event_type as kwarg for channels that want it (webhook_generic)
                result = channel.send(
                    address=address,
                    title=event.title,
                    body=event.body,
                    event_type=event.event_type,
                )
            except TypeError:
                # Fallback for channels that don't accept event_type kwarg
                result = channel.send(address=address, title=event.title, body=event.body)

            if result.delivered:
                delivered.append(channel_type)
            else:
                errors[channel_type] = result.error or "unknown"

        self.db.record_notification(
            recipient_dev_id=event.recipient_dev_id,
            event_type=event.event_type,
            title=event.title,
            body=event.body,
            job_id=event.job_id,
            channels_attempted=attempted,
            channels_delivered=delivered,
            delivery_errors=errors,
            metadata=event.metadata,
        )

        return RouterResult(
            channels_attempted=attempted,
            channels_delivered=delivered,
            errors=errors,
        )

    def send_multi(self, event: NotificationEvent) -> list[RouterResult]:
        """For events that notify multiple devs (e.g., lock conflicts)."""
        results = [self.send(event)]

        if event.event_type == "lock_conflict":
            blocking_dev_id = event.metadata.get("blocking_dev_id")
            if blocking_dev_id and blocking_dev_id != event.recipient_dev_id:
                blocker_event = NotificationEvent(
                    event_type="lock_conflict",
                    recipient_dev_id=blocking_dev_id,
                    title=f"Your job is blocking {event.recipient_dev_id}",
                    body=(
                        f"Your factory job is holding file locks that another dev's job needs.\n\n"
                        f"{event.body}"
                    ),
                    job_id=event.job_id,
                    metadata=event.metadata,
                )
                results.append(self.send(blocker_event))

        return results
```

**Step 3: Run tests**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_notification_router.py -v`
Expected: All tests PASS

**Step 4: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/notifications/router.py factory/tests/test_notification_router.py
git commit -m "feat: add NotificationRouter with per-dev channel fan-out"
```

---

## Task 12: Cleanup Agent Integration

**Files:**
- Modify: `factory/cleanup_agent.py`

**Step 1: Add imports and wire up notifications**

Add to imports in `factory/cleanup_agent.py`:

```python
from notifications.router import NotificationRouter, NotificationEvent
```

**Step 2: Fire notifications in `run_post_cleanup`**

At the end of `run_post_cleanup` (after file lock release), add:

```python
        # Fire notification for this job's terminal state
        try:
            router = NotificationRouter(self.db)
            event_type = self._event_type_for_status(job.status)
            if event_type and job.submitted_by:
                event = NotificationEvent(
                    event_type=event_type,
                    recipient_dev_id=job.submitted_by,
                    title=self._notification_title(job, event_type),
                    body=report.summary,
                    job_id=job.id,
                    metadata={
                        "final_status": job.status.value,
                        "error_count": job.error_count,
                    },
                )
                router.send(event)
        except Exception as e:
            logger.warning(
                "Notification dispatch failed for job %s: %s (non-blocking)",
                job_id[:8], e,
            )
```

Add helper methods to `CleanupAgent`:

```python
    def _event_type_for_status(self, status: JobStatus) -> str | None:
        return {
            JobStatus.READY_FOR_APPROVAL: "job_ready",
            JobStatus.FAILED: "job_failed",
        }.get(status)

    def _notification_title(self, job: FactoryJob, event_type: str) -> str:
        return {
            "job_ready": f"✅ Job ready for review: {job.title}",
            "job_failed": f"❌ Job failed: {job.title}",
            "unblocked": f"🔓 Job unblocked: {job.title}",
            "needs_human": f"🤔 Job needs human input: {job.title}",
        }.get(event_type, f"Job update: {job.title}")
```

**Step 3: Fire needs_human in attempt_recovery**

In `attempt_recovery`, before returning a `needs_human` report, add:

```python
            try:
                router = NotificationRouter(self.db)
                if job.submitted_by:
                    router.send(NotificationEvent(
                        event_type="needs_human",
                        recipient_dev_id=job.submitted_by,
                        title=f"🤔 Job needs human input: {job.title}",
                        body=report.summary,
                        job_id=job.id,
                    ))
            except Exception as e:
                logger.warning("needs_human notification failed: %s", e)
```

**Step 4: Run full test suite**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/ -v`
Expected: All tests pass

**Step 5: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/cleanup_agent.py
git commit -m "feat: wire NotificationRouter into cleanup agent"
```

---

## Task 13: Orchestrator Lock Conflict + Unblock Notifications

**Files:**
- Modify: `factory/orchestrator.py`

**Step 1: Fire `lock_conflict` in `_run_planning` WAITING branch**

In the lock conflict block of `_run_planning`, look up blocking dev and fire notification:

```python
            if not lock_result.success:
                blocking_job_id = lock_result.conflicts[0]["blocking_job_id"] if lock_result.conflicts else None
                blocking_dev_id = None
                if blocking_job_id:
                    blocking_job = self.db.get_job(blocking_job_id)
                    if blocking_job:
                        blocking_dev_id = blocking_job.submitted_by

                # ... existing logging, artifact storage, SQL update, transition ...

                # Fire notification to both devs
                try:
                    from notifications.router import NotificationRouter, NotificationEvent
                    router = NotificationRouter(self.db)
                    if job.submitted_by:
                        conflict_files = [c["file_path"] for c in lock_result.conflicts]
                        router.send_multi(NotificationEvent(
                            event_type="lock_conflict",
                            recipient_dev_id=job.submitted_by,
                            title=f"🔒 Job waiting on file locks: {job.title}",
                            body=(
                                "Your job is blocked by another dev's job.\n\n"
                                "Conflicting files:\n" +
                                "\n".join(f"  • {f}" for f in conflict_files) +
                                (f"\n\nBlocking dev: {blocking_dev_id}" if blocking_dev_id else "")
                            ),
                            job_id=job.id,
                            metadata={
                                "blocking_dev_id": blocking_dev_id,
                                "blocking_job_id": blocking_job_id,
                                "conflicts": lock_result.conflicts,
                            },
                        ))
                except Exception as e:
                    logger.warning("Lock conflict notification failed: %s", e)
```

**Step 2: Fire `unblocked` in `_run_waiting` success path**

In `_run_waiting`, after successful lock acquisition:

```python
            if lock_result.success:
                # ... existing branch creation and transition logic ...

                try:
                    from notifications.router import NotificationRouter, NotificationEvent
                    router = NotificationRouter(self.db)
                    if job.submitted_by:
                        router.send(NotificationEvent(
                            event_type="unblocked",
                            recipient_dev_id=job.submitted_by,
                            title=f"🔓 Job unblocked: {job.title}",
                            body="Your job is no longer blocked on file locks and is now implementing.",
                            job_id=job.id,
                        ))
                except Exception as e:
                    logger.warning("Unblock notification failed: %s", e)
```

**Step 3: Run tests**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/ -v`
Expected: All pass

**Step 4: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/orchestrator.py
git commit -m "feat: fire lock_conflict and unblocked notifications in orchestrator"
```

---

## Task 14: devbrain CLI — register, history (NL), watch, telegram-discover

**Files:**
- Create: `factory/cli.py`
- Create: `bin/devbrain`
- Test: `factory/tests/test_cli.py`

**Step 1: Install click**

Run: `cd /Users/patrickkelly/devbrain && .venv/bin/pip install click`
Expected: installed

**Step 2: Write the failing test**

Create `factory/tests/test_cli.py`:

```python
"""Tests for the devbrain CLI."""
import pytest
from click.testing import CliRunner
from cli import cli


@pytest.fixture
def runner():
    return CliRunner()


def test_register_with_single_channel(runner):
    result = runner.invoke(cli, [
        "register",
        "--dev-id", "test_cli_reg",
        "--name", "CLI Test",
        "--channel", "tmux:test_cli_reg",
    ])
    assert result.exit_code == 0
    assert "registered" in result.output.lower()


def test_register_with_multiple_channels(runner):
    result = runner.invoke(cli, [
        "register",
        "--dev-id", "test_cli_multi",
        "--channel", "tmux:test_cli_multi",
        "--channel", "smtp:test@example.com",
        "--channel", "webhook_slack:https://hooks.slack.com/test",
    ])
    assert result.exit_code == 0


def test_history_command(runner):
    result = runner.invoke(cli, [
        "history", "--dev", "test_cli_reg", "--recent", "5",
    ])
    assert result.exit_code == 0


def test_history_nl_dry_run_ollama_unavailable(runner):
    """NL query gracefully handles ollama unavailable."""
    result = runner.invoke(cli, [
        "history", "--query", "failed jobs this week", "--dry-run",
    ])
    # Either succeeds (ollama running) or shows clear error
    assert result.exit_code in (0, 1)
```

**Step 3: Implement cli.py**

Create `factory/cli.py`:

```python
"""DevBrain CLI — dev registration, notification history, telegram setup."""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

import click
import yaml

from state_machine import FactoryDB

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"
OLLAMA_URL = "http://localhost:11434"
NL_MODEL = "qwen2.5:7b"


def get_db() -> FactoryDB:
    return FactoryDB(os.environ.get("DEVBRAIN_DATABASE_URL", DATABASE_URL))


def parse_channel(s: str) -> dict:
    """Parse --channel TYPE:ADDRESS into a channel dict."""
    if ":" not in s:
        raise click.BadParameter(f"Channel must be TYPE:ADDRESS, got: {s}")
    ch_type, address = s.split(":", 1)
    return {"type": ch_type.strip(), "address": address.strip()}


@click.group()
def cli():
    """DevBrain CLI — manage devs and notifications."""
    pass


@cli.command()
@click.option("--dev-id", default=None, help="SSH username (defaults to $USER)")
@click.option("--name", default=None, help="Full name")
@click.option(
    "--channel", "channels", multiple=True,
    help="Channel in format TYPE:ADDRESS (can repeat). "
         "Types: tmux, smtp, gmail_dwd, gchat_dwd, telegram_bot, "
         "webhook_slack, webhook_discord, webhook_generic",
)
def register(dev_id: str | None, name: str | None, channels: tuple):
    """Register yourself (or another dev) for notifications."""
    dev_id = dev_id or os.environ.get("USER")
    if not dev_id:
        click.echo("Error: --dev-id required (or set $USER)", err=True)
        sys.exit(1)

    parsed_channels = [parse_channel(c) for c in channels]

    db = get_db()
    db.register_dev(dev_id=dev_id, full_name=name, channels=parsed_channels)

    click.echo(f"✅ Dev '{dev_id}' registered with {len(parsed_channels)} channel(s).")
    for c in parsed_channels:
        click.echo(f"   • {c['type']}: {c['address']}")


@cli.command(name="add-channel")
@click.option("--dev-id", default=None)
@click.option("--channel", "channel_spec", required=True, help="TYPE:ADDRESS")
def add_channel(dev_id: str | None, channel_spec: str):
    """Add a channel to an existing dev registration."""
    dev_id = dev_id or os.environ.get("USER")
    db = get_db()
    ch = parse_channel(channel_spec)
    db.add_dev_channel(dev_id, ch)
    click.echo(f"✅ Added {ch['type']}:{ch['address']} to {dev_id}")


@cli.command()
@click.option("--dev", default=None, help="Filter by dev_id (defaults to $USER)")
@click.option("--job", "job_id", default=None, help="Filter by job ID")
@click.option("--event", default=None, help="Filter by event_type")
@click.option("--since", default=None, help="Time window: 1h, 1d, 1w")
@click.option("--recent", default=None, type=int, help="Show N most recent")
@click.option("--query", "nl_query", default=None, help="Natural language query (uses ollama)")
@click.option("--dry-run", is_flag=True, help="For --query: show SQL without executing")
@click.option("--json", "as_json", is_flag=True)
def history(dev, job_id, event, since, recent, nl_query, dry_run, as_json):
    """Browse notification history."""
    db = get_db()

    if nl_query:
        _run_nl_history(db, nl_query, dry_run, as_json)
        return

    since_hours = None
    if since:
        m = re.match(r"(\d+)([hdwm])", since)
        if m:
            num, unit = int(m.group(1)), m.group(2)
            since_hours = num * {"h": 1, "d": 24, "w": 168, "m": 720}[unit]

    if not dev and not job_id and not event and not recent:
        dev = os.environ.get("USER")

    notifs = db.get_notifications(
        recipient_dev_id=dev,
        job_id=job_id,
        event_type=event,
        since_hours=since_hours,
        limit=recent or 50,
    )

    if as_json:
        click.echo(json.dumps(notifs, indent=2, default=str))
        return

    if not notifs:
        click.echo("No notifications found.")
        return

    for n in notifs:
        icon = "✅" if n["channels_delivered"] else "⚠️"
        click.echo(f"\n{icon}  [{n['sent_at'][:19]}] {n['event_type']}")
        click.echo(f"   {n['title']}")
        if n["body"]:
            body = n["body"][:200]
            click.echo(f"   {body}{'...' if len(n['body']) > 200 else ''}")
        if n["channels_delivered"]:
            click.echo(f"   Delivered: {', '.join(n['channels_delivered'])}")
        if n["delivery_errors"]:
            errs = ", ".join(f"{k}: {v[:50]}" for k, v in n["delivery_errors"].items())
            click.echo(f"   Errors: {errs}")


def _run_nl_history(db, query, dry_run, as_json):
    schema = """
CREATE TABLE devbrain.notifications (
    id UUID, recipient_dev_id VARCHAR, job_id UUID,
    event_type VARCHAR, title VARCHAR, body TEXT,
    channels_attempted JSONB, channels_delivered JSONB,
    delivery_errors JSONB, sent_at TIMESTAMPTZ, metadata JSONB
);

CREATE TABLE devbrain.factory_jobs (
    id UUID, title VARCHAR, status VARCHAR, submitted_by VARCHAR, created_at TIMESTAMPTZ
);
"""
    prompt = f"""Convert this natural language query into a single PostgreSQL SELECT.

SCHEMA:
{schema}

QUERY: {query}

RULES:
- Only SELECT, never mutations
- Always LIMIT 50
- Order by sent_at DESC unless specified
- Use 'now() - interval' for time filters
- Prefix tables with devbrain.
- Output ONLY SQL, no explanation, no markdown

SQL:"""

    try:
        data = json.dumps({
            "model": NL_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.1},
        }).encode()
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        sql = result["response"].strip()
        sql = re.sub(r"^```sql\s*|\s*```$", "", sql, flags=re.MULTILINE).strip()
    except Exception as e:
        click.echo(f"Error calling ollama at {OLLAMA_URL}: {e}", err=True)
        click.echo("Is ollama running?", err=True)
        sys.exit(1)

    if not re.match(r"^\s*SELECT", sql, re.IGNORECASE):
        click.echo(f"Error: generated SQL is not a SELECT:\n{sql}", err=True)
        sys.exit(1)

    if dry_run:
        click.echo(f"Generated SQL:\n{sql}")
        return

    click.echo(f"Running: {sql[:200]}{'...' if len(sql) > 200 else ''}\n")

    with db._conn() as conn, conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
        colnames = [d[0] for d in cur.description] if cur.description else []

    if as_json:
        results = [dict(zip(colnames, r)) for r in rows]
        click.echo(json.dumps(results, indent=2, default=str))
        return

    if not rows:
        click.echo("No results.")
        return

    for row in rows:
        click.echo(str(dict(zip(colnames, row))))


@cli.command()
@click.option("--dev", default=None)
def watch(dev: str | None):
    """Tail live notifications (polls every 5s)."""
    dev = dev or os.environ.get("USER")
    db = get_db()
    click.echo(f"Watching notifications for {dev} (Ctrl-C to stop)...\n")
    last_id = None
    try:
        while True:
            notifs = db.get_notifications(recipient_dev_id=dev, limit=5)
            new = []
            for n in notifs:
                if last_id and n["id"] == last_id:
                    break
                new.append(n)
            for n in reversed(new):
                click.echo(f"[{n['sent_at'][:19]}] {n['event_type']}: {n['title']}")
            if notifs:
                last_id = notifs[0]["id"]
            time.sleep(5)
    except KeyboardInterrupt:
        click.echo("\nStopped.")


@cli.command(name="telegram-discover")
@click.option("--dev-id", default=None)
@click.option("--username", default=None, help="Your Telegram username (optional, helps filter)")
def telegram_discover(dev_id: str | None, username: str | None):
    """Auto-discover your Telegram chat_id and save it."""
    dev_id = dev_id or os.environ.get("USER")
    if not dev_id:
        click.echo("Error: --dev-id required", err=True)
        sys.exit(1)

    # Load bot token from config
    config_path = Path(__file__).parent.parent / "config" / "devbrain.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f) or {}
    tg_config = config.get("notifications", {}).get("channels", {}).get("telegram_bot", {})
    bot_token = tg_config.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN")
    bot_username = tg_config.get("bot_username", "your bot")

    if not bot_token:
        click.echo("Error: Telegram bot token not set in config or TELEGRAM_BOT_TOKEN env var", err=True)
        sys.exit(1)

    click.echo(f"Step 1: On Telegram, DM @{bot_username} with any message (e.g., 'hi').")
    click.input("Step 2: Press Enter here when you've sent the message...")

    from notifications.channels.telegram_bot import TelegramBotChannel
    channel = TelegramBotChannel(bot_token=bot_token)
    chat_id = channel.discover_chat_id(username_hint=username)

    if not chat_id:
        click.echo("❌ Could not find your chat. Make sure you DM'd the bot first.", err=True)
        sys.exit(1)

    # Save to dev's channels
    db = get_db()
    dev = db.get_dev(dev_id)
    if not dev:
        db.register_dev(dev_id=dev_id, channels=[{"type": "telegram_bot", "address": chat_id}])
    else:
        db.add_dev_channel(dev_id, {"type": "telegram_bot", "address": chat_id})

    click.echo(f"✅ Telegram chat_id '{chat_id}' saved for {dev_id}")

    # Send a test message
    click.echo("Sending test message...")
    result = channel.send(chat_id, "DevBrain Setup Complete", "You're now registered for Telegram notifications.")
    if result.delivered:
        click.echo("✅ Test message delivered.")
    else:
        click.echo(f"⚠️  Test failed: {result.error}")


if __name__ == "__main__":
    cli()
```

**Step 4: Create shell wrapper**

Create `bin/devbrain`:

```bash
#!/bin/bash
set -e
DEVBRAIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$DEVBRAIN_DIR/.venv/bin/python" "$DEVBRAIN_DIR/factory/cli.py" "$@"
```

Run: `chmod +x /Users/patrickkelly/devbrain/bin/devbrain`

**Step 5: Run tests and smoke test**

```bash
cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/test_cli.py -v
/Users/patrickkelly/devbrain/bin/devbrain --help
```

**Step 6: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/cli.py factory/tests/test_cli.py bin/devbrain requirements.txt
git commit -m "feat: add devbrain CLI with register, history (NL), watch, telegram-discover"
```

---

## Task 15: devbrain_notify MCP Tool

**Files:**
- Modify: `mcp-server/src/index.ts`
- Create: `factory/notify_cli.py` (thin subprocess entrypoint)

**Step 1: Create the subprocess entrypoint**

Create `factory/notify_cli.py`:

```python
"""Subprocess entrypoint for sending notifications from the MCP server.

Called as: python notify_cli.py <recipient_dev_id> <event_type> <title_file> <body_file>
Reads title and body from files to avoid shell escaping issues.
"""

from __future__ import annotations

import sys
from pathlib import Path

from state_machine import FactoryDB
from notifications.router import NotificationRouter, NotificationEvent

DATABASE_URL = "postgresql://devbrain:devbrain-local@localhost:5433/devbrain"


def main():
    if len(sys.argv) < 5:
        print("Usage: notify_cli.py <recipient> <event_type> <title_file> <body_file>", file=sys.stderr)
        sys.exit(1)

    recipient = sys.argv[1]
    event_type = sys.argv[2]
    title = Path(sys.argv[3]).read_text()
    body = Path(sys.argv[4]).read_text()

    db = FactoryDB(DATABASE_URL)
    router = NotificationRouter(db)
    result = router.send(NotificationEvent(
        event_type=event_type,
        recipient_dev_id=recipient,
        title=title,
        body=body,
    ))

    if result.skipped:
        print("skipped")
    else:
        print(f"attempted={','.join(result.channels_attempted)}")
        print(f"delivered={','.join(result.channels_delivered)}")


if __name__ == "__main__":
    main()
```

**Step 2: Add devbrain_notify MCP tool**

In `mcp-server/src/index.ts`, add after `factory_cleanup`:

```typescript
// ─── Tool: devbrain_notify ─────────────────────────────────────────────

import { writeFileSync, unlinkSync } from 'fs'
import { tmpdir } from 'os'
import { join } from 'path'

server.tool(
  'devbrain_notify',
  'Send a notification to a registered dev through their configured channels. Use for agent-driven notifications during factory runs.',
  {
    recipient: z.string().describe('dev_id of the recipient'),
    event_type: z.enum([
      'job_ready', 'job_failed', 'lock_conflict',
      'unblocked', 'needs_human',
    ]),
    title: z.string(),
    body: z.string(),
  },
  async ({ recipient, event_type, title, body }) => {
    // Write title and body to temp files to avoid shell escaping
    const titleFile = join(tmpdir(), `devbrain-notif-title-${Date.now()}.txt`)
    const bodyFile = join(tmpdir(), `devbrain-notif-body-${Date.now()}.txt`)
    writeFileSync(titleFile, title)
    writeFileSync(bodyFile, body)

    try {
      const pythonBin = resolve(import.meta.dirname, '../../.venv/bin/python')
      const notifyScript = resolve(import.meta.dirname, '../../factory/notify_cli.py')

      const { spawnSync } = await import('child_process')
      const result = spawnSync(
        pythonBin,
        [notifyScript, recipient, event_type, titleFile, bodyFile],
        { encoding: 'utf-8' },
      )

      return {
        content: [{
          type: 'text',
          text: result.stdout || result.stderr || 'No output',
        }],
      }
    } finally {
      try { unlinkSync(titleFile) } catch {}
      try { unlinkSync(bodyFile) } catch {}
    }
  },
)
```

**Step 3: Build and commit**

```bash
cd /Users/patrickkelly/devbrain/mcp-server && npm run build
cd /Users/patrickkelly/devbrain
git add mcp-server/src/index.ts factory/notify_cli.py
git commit -m "feat: add devbrain_notify MCP tool for agent-driven notifications"
```

---

## Task 16: Setup Documentation

**Files:**
- Create: `docs/notifications/README.md`
- Create: `docs/notifications/tmux.md`
- Create: `docs/notifications/smtp.md`
- Create: `docs/notifications/gmail-dwd.md`
- Create: `docs/notifications/gchat-dwd.md`
- Create: `docs/notifications/telegram.md`
- Create: `docs/notifications/webhooks.md`

**Step 1: Create docs directory and README**

Create `docs/notifications/README.md` — overview listing all channels with links to per-channel setup guides.

**Step 2: Create per-channel setup docs**

Each file contains:
- Prerequisites (account type, tokens, APIs to enable)
- Step-by-step setup
- Config snippet to add to devbrain.yaml
- How to register as a dev with this channel
- Troubleshooting

Keep each file focused and ~30-60 lines. Write them pragmatically based on how each channel works.

**Step 3: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add docs/notifications/
git commit -m "docs: add setup guides for all notification channels"
```

---

## Task 17: Integration Tests

**Files:**
- Create: `factory/tests/test_notifications_integration.py`

**Step 1: Write end-to-end tests**

Cover the full flow:
- Dev registers with multiple channels
- Cleanup agent fires job_ready → notification recorded + channels attempted
- Lock conflict fires → both devs get notified
- Router respects event subscriptions
- NL history query works (if ollama available)

**Step 2: Run full suite**

Run: `cd /Users/patrickkelly/devbrain/factory && ../.venv/bin/python -m pytest tests/ -v`
Expected: All tests pass

**Step 3: Commit**

```bash
cd /Users/patrickkelly/devbrain
git add factory/tests/test_notifications_integration.py
git commit -m "test: add end-to-end notifications integration tests"
```

---

## Summary

| Task | What | Depends On |
|------|------|-----------|
| 1 | DB migration: devs + notifications | — |
| 2 | Generic config template | — |
| 3 | Devs + notifications CRUD | 1 |
| 4 | NotificationChannel base + registry | — |
| 5 | Tmux channel | 4 |
| 6 | SMTP channel | 4 |
| 7 | Gmail DWD channel | 4 |
| 8 | Google Chat DWD channel | 4 |
| 9 | Telegram bot channel + auto-discover | 4 |
| 10 | Webhook channels (Slack/Discord/generic) | 4 |
| 11 | NotificationRouter | 3, 5, 6, 7, 8, 9, 10 |
| 12 | Cleanup agent integration | 11 |
| 13 | Orchestrator integration | 11 |
| 14 | devbrain CLI | 3, 9 |
| 15 | devbrain_notify MCP tool | 11 |
| 16 | Setup docs | 5-10 |
| 17 | Integration tests | 12, 13 |

**Parallelization opportunities:**
- Tasks 3 and 4 can run together (after 1)
- Tasks 5-10 can all run in parallel (after 4)
- Task 12 and 13 can run in parallel (after 11)

**Design principles:**

1. **Provider-agnostic** — no hardcoded company names, emails, or credentials anywhere
2. **Pluggable** — adding a new channel is a single file that registers with `default_registry`
3. **Graceful degradation** — missing credentials disable channels silently, tmux always works
4. **Env var overrides** — sensitive values (SMTP creds, Telegram token) can use env vars
5. **Per-dev choice** — devs pick their own channels via `devbrain register --channel TYPE:ADDRESS`
6. **Agent-agnostic notifications** — tmux popup works for any AI CLI, no hooks or tool-specific code
7. **MCP escape hatch** — `devbrain_notify` tool lets AI agents push notifications during factory runs without needing to know about the channels

**Manual setup per user (documented in Task 16):**

- **Tmux**: None — works if tmux is installed
- **SMTP**: Create an app password with your email provider, add to config
- **Gmail DWD / Chat DWD**: GCP service account + domain-wide delegation + scope grant in Workspace admin
- **Telegram**: Create bot via @BotFather, add token to config, run `devbrain telegram-discover`
- **Slack webhook**: Create incoming webhook in Slack admin, register with `--channel webhook_slack:URL`
- **Discord webhook**: Same as Slack
- **Generic webhook**: Any HTTP endpoint accepting JSON POSTs

**Out of scope (follow-up plans):**
- Real-time dashboard (Textual TUI) — separate plan
- Mac Studio deployment automation — separate plan
- Slack/Discord/Telegram *MCP servers* (as opposed to notification channels) — use existing community ones if needed
