-- Migration: Devs + Notifications
-- Adds devs table (developer registry with notification channel preferences)
-- and notifications table (history of notifications sent to devs).
--
-- Usage:
--   docker exec -i devbrain-db psql -U devbrain -d devbrain < migrations/005_notifications.sql

-- ─── Devs: Developer Registry with Notification Channels ─────────────────────

CREATE TABLE IF NOT EXISTS devbrain.devs (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    dev_id                  VARCHAR(255) UNIQUE NOT NULL,
    full_name               VARCHAR(255),
    channels                JSONB DEFAULT '[]',
    event_subscriptions     JSONB DEFAULT '["job_ready","job_failed","lock_conflict","unblocked","needs_human"]',
    created_at              TIMESTAMPTZ DEFAULT now(),
    updated_at              TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_devs_dev_id
    ON devbrain.devs(dev_id);

-- ─── Notifications: History of Sent Notifications ────────────────────────────

CREATE TABLE IF NOT EXISTS devbrain.notifications (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recipient_dev_id        VARCHAR(255) NOT NULL,
    job_id                  UUID REFERENCES devbrain.factory_jobs(id) ON DELETE SET NULL,
    event_type              VARCHAR(50) NOT NULL,
    title                   VARCHAR(500) NOT NULL,
    body                    TEXT NOT NULL,
    channels_attempted      JSONB DEFAULT '[]',
    channels_delivered      JSONB DEFAULT '[]',
    delivery_errors         JSONB DEFAULT '{}',
    sent_at                 TIMESTAMPTZ DEFAULT now(),
    metadata                JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_notifications_recipient
    ON devbrain.notifications(recipient_dev_id);

CREATE INDEX IF NOT EXISTS idx_notifications_job
    ON devbrain.notifications(job_id);

CREATE INDEX IF NOT EXISTS idx_notifications_sent_at
    ON devbrain.notifications(sent_at DESC);

CREATE INDEX IF NOT EXISTS idx_notifications_event_type
    ON devbrain.notifications(event_type);
