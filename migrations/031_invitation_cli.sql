-- ─────────────────────────────────────────────────────────────────────────────
-- 031: Add cli column to devbrain.invitations
-- ─────────────────────────────────────────────────────────────────────────────
--
-- Records which AI CLI the dev was invited to use. The reconciler and
-- rotate handler use this to stash credentials at the correct per-profile
-- path and apply the right validation logic.
--
-- Valid values: 'claude' | 'codex' | 'gemini'
-- Default: 'claude' — preserves behavior for all pre-031 invitations.
--
-- Single-CLI per invitation: a dev who uses multiple CLIs gets one
-- invitation per CLI (or re-invites once the first is activated). This
-- keeps the rotation handler simple — one credential type per rotation
-- session — and matches the rotate.sh stdin schema.

ALTER TABLE devbrain.invitations
    ADD COLUMN IF NOT EXISTS cli VARCHAR(32) NOT NULL DEFAULT 'claude'
    CHECK (cli IN ('claude', 'codex', 'gemini'));

COMMENT ON COLUMN devbrain.invitations.cli IS
    'AI CLI the dev is being onboarded for. '
    'Determines install/login instructions in the kit and the credential '
    'storage path used by the reconciler. Added in migration 031.';

INSERT INTO devbrain.schema_migrations (filename)
VALUES ('031_invitation_cli.sql')
ON CONFLICT (filename) DO NOTHING;
