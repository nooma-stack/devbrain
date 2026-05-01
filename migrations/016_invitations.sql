-- DevBrain Onboarding Invitations
-- ============================================================================
--
-- Tracks pending dev onboarding flows. The admin runs `devbrain setup
-- add-dev` to stage a new dev: this creates a row here with a single-use
-- token. The dev (or their agent) then submits a pubkey and an OAuth
-- token via the webhook receiver service; both land back in this row.
-- A reconciliation worker watches for fully-supplied rows and applies
-- them: writes pubkey to authorized_keys, stashes oauth-token at the
-- per-profile path, marks status=activated, fires a notification to the
-- admin.
--
-- Token storage: we never store the raw invite token. Only its SHA-256
-- hash. The raw token lives exactly twice — in the .md file we hand the
-- dev (transient), and in the URL the dev's agent POSTs to (transient).
-- Loss of the DB row + hash leaks nothing about the token.
--
-- OAuth token storage: the raw `sk-ant-oat01-...` token is sensitive.
-- We store it briefly here so the reconciler can move it to the
-- per-profile file (mode 600), then NULL out this column. So the DB
-- holds the token only for the seconds between webhook-receipt and
-- reconciler-action. RLS would be nice; we rely on Postgres role
-- separation instead (devbrain role can read; webhook service writes).

CREATE TABLE IF NOT EXISTS devbrain.invitations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- The dev being onboarded. References devs.dev_id but NOT a foreign
    -- key — we want the row to survive even if the dev row is later
    -- deleted (audit trail). Validated by app code via the regex used
    -- elsewhere ([a-z0-9_-]{1,64}).
    dev_id          VARCHAR(64) NOT NULL,
    -- SHA-256 of the raw invite token (`dvbn_inv_<random>`). Hex-encoded
    -- (64 chars). Lookups by token: hash the input and compare.
    token_hash      CHAR(64) NOT NULL,
    -- Where the dev's pubkey + oauth-token will arrive. Populated after
    -- the dev's agent POSTs to the webhook. NULL until then.
    pubkey          TEXT,
    pubkey_received_at  TIMESTAMPTZ,
    oauth_token     TEXT,  -- NULLed by reconciler after move to file
    oauth_token_received_at  TIMESTAMPTZ,
    -- Status machine: pending → ready (both received) → activated → archived
    -- (failure paths: expired, revoked).
    status          VARCHAR(32) NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'ready', 'activated', 'expired', 'revoked')),
    -- Whether the reconciler should auto-activate (default) or wait for
    -- explicit `devbrain setup activate --dev <id>`. Per-invite override
    -- of the global default in case of high-trust auto-onboard vs
    -- security-sensitive manual review.
    auto_activate   BOOLEAN NOT NULL DEFAULT TRUE,
    -- The email address the .md was sent to. NULL if delivery is manual.
    email           VARCHAR(320),
    -- Free-form note from `devbrain setup add-dev` (e.g., "BrightBot
    -- joining 2026-05-15", "external auditor, 2-week TTL").
    notes           TEXT,
    created_by      VARCHAR(64),  -- macOS user / dev_id of admin
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    activated_at    TIMESTAMPTZ
);

-- Lookups:
--   * webhook receiver hashes URL token, hits this index for validation
CREATE UNIQUE INDEX IF NOT EXISTS invitations_token_hash_idx
    ON devbrain.invitations (token_hash);
--   * reconciler scans for rows ready to apply
CREATE INDEX IF NOT EXISTS invitations_status_idx
    ON devbrain.invitations (status)
    WHERE status IN ('pending', 'ready');
--   * `devbrain setup invitations` admin view, by dev
CREATE INDEX IF NOT EXISTS invitations_dev_id_idx
    ON devbrain.invitations (dev_id, created_at DESC);
