-- Atlas Step 7 — Seeded compliance rules (HIPAA + SOC2 + FERPA)
-- ============================================================================
--
-- Five regulatory-grade rules across 3 compliance profiles. Each ships with
-- a postulate test that proves its enforcement contract.
--
-- Profile distribution: 2 hipaa + 1 ferpa + 2 soc2.
--
-- Rules use the canonical 'devbrain' project (slug='devbrain') so they're
-- available substrate. Project enablement (compliance_profiles_enabled)
-- determines which projects get them in their briefs (per Step 7b filter).
--
-- The lookup uses slug (not name) because slug is the canonical stable
-- identifier — name is a human-friendly label ('DevBrain') and slug is the
-- machine identifier ('devbrain').

INSERT INTO devbrain.memory
    (project_id, kind, title, content, tier, strength, compliance_profiles)
SELECT
    (SELECT id FROM devbrain.projects WHERE slug = 'devbrain' LIMIT 1),
    kind,
    title,
    content,
    'rule',
    1.0,
    profiles
FROM (VALUES
    ('decision'::text,
     'PHI columns must not appear in unstructured logs',
     'Reads/writes against phi_* tables must redact column values before any logger call. Loggers (info/debug/warn/error) emitting raw PHI table contents are a HIPAA 164.312(a)(2)(iv) violation.',
     ARRAY['hipaa']::text[]),
    ('decision',
     'Audit log writes required for every PHI read/write at service-method boundary',
     'HIPAA 164.312(b) audit controls — every service method that reads or writes phi_* tables must emit an audit_log row with actor + action + resource. Method-level enforcement (not row-level): one audit_log entry per service-method invocation.',
     ARRAY['hipaa']::text[]),
    ('decision',
     'Secrets must not appear in error messages or stack traces',
     'SOC2 CC6.1 — exception paths must redact password=, token=, api_key=, secret=, bearer + similar literal substrings before propagating to logs or HTTP responses. Stack-trace serialization must scrub frame locals of the same patterns.',
     ARRAY['soc2']::text[]),
    ('decision',
     'Student identifiers (SSID, full name) must not leave the EMR boundary in unredacted form',
     'FERPA 99.31 — any function returning a student.ssid or student.full_name to a non-EMR caller (Google Workspace integration, public API, third-party webhook) must redact or hash the identifier. Within the EMR boundary, raw identifiers are permitted.',
     ARRAY['ferpa']::text[]),
    ('decision',
     'Bulk exports require explicit DPO sign-off claim in the request',
     'SOC2 CC6.7 — endpoints with bulk export semantics (returning >100 rows of regulated data) must validate a request claim signed by the DPO role. Single-record reads are exempt; the threshold is per-request payload size.',
     ARRAY['soc2']::text[])
) AS r(kind, title, content, profiles)
ON CONFLICT DO NOTHING;
