-- DevBrain Initial Schema
-- =======================
-- Universal persistent memory + dev factory tables.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE SCHEMA IF NOT EXISTS devbrain;

-- ─── Project Registry ────────────────────────────────────────────────────────

CREATE TABLE devbrain.projects (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug            VARCHAR(100) UNIQUE NOT NULL,
    name            VARCHAR(255) NOT NULL,
    root_path       TEXT,
    description     TEXT,
    constraints     JSONB DEFAULT '[]',
    tech_stack      JSONB DEFAULT '{}',
    lint_commands   JSONB DEFAULT '{}',
    test_commands   JSONB DEFAULT '{}',
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- ─── Raw Sessions (lossless transcript storage) ──────────────────────────────

CREATE TABLE devbrain.raw_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID REFERENCES devbrain.projects(id),
    source_app      VARCHAR(50) NOT NULL,
    source_path     TEXT NOT NULL,
    source_hash     VARCHAR(64) NOT NULL,
    session_id      VARCHAR(255),
    model_used      VARCHAR(100),
    started_at      TIMESTAMPTZ,
    ended_at        TIMESTAMPTZ,
    message_count   INT,
    raw_content     TEXT NOT NULL,
    summary         TEXT,
    files_touched   JSONB DEFAULT '[]',
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE(source_app, source_hash)
);

CREATE INDEX idx_raw_sessions_project ON devbrain.raw_sessions(project_id);
CREATE INDEX idx_raw_sessions_app ON devbrain.raw_sessions(source_app);
CREATE INDEX idx_raw_sessions_started ON devbrain.raw_sessions(started_at DESC);

-- ─── Embedded Chunks (searchable segments) ───────────────────────────────────

CREATE TABLE devbrain.chunks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID REFERENCES devbrain.projects(id),
    source_type     VARCHAR(50) NOT NULL,
    source_id       UUID,
    source_line_start INT,
    source_line_end   INT,
    content         TEXT NOT NULL,
    embedding       vector(1024),
    token_count     INT,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_chunks_project ON devbrain.chunks(project_id);
CREATE INDEX idx_chunks_source_type ON devbrain.chunks(source_type);
CREATE INDEX idx_chunks_source_id ON devbrain.chunks(source_id);

-- IVFFlat index created after initial data load for better build quality.
-- Run manually after migration: see migrations/002_create_vector_indexes.sql

-- ─── Architecture Decisions ──────────────────────────────────────────────────

CREATE TABLE devbrain.decisions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID REFERENCES devbrain.projects(id),
    title           VARCHAR(500) NOT NULL,
    context         TEXT NOT NULL,
    decision        TEXT NOT NULL,
    rationale       TEXT,
    alternatives    JSONB DEFAULT '[]',
    constraints     JSONB DEFAULT '[]',
    status          VARCHAR(50) DEFAULT 'active',
    superseded_by   UUID REFERENCES devbrain.decisions(id),
    session_id      UUID REFERENCES devbrain.raw_sessions(id),
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_decisions_project ON devbrain.decisions(project_id);
CREATE INDEX idx_decisions_status ON devbrain.decisions(status);

-- ─── Reusable Patterns ───────────────────────────────────────────────────────

CREATE TABLE devbrain.patterns (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID REFERENCES devbrain.projects(id),
    name            VARCHAR(255) NOT NULL,
    category        VARCHAR(100),
    description     TEXT NOT NULL,
    example_code    TEXT,
    files           JSONB DEFAULT '[]',
    tags            JSONB DEFAULT '[]',
    session_id      UUID REFERENCES devbrain.raw_sessions(id),
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_patterns_project ON devbrain.patterns(project_id);
CREATE INDEX idx_patterns_category ON devbrain.patterns(category);

-- ─── Issues & Lessons Learned ────────────────────────────────────────────────

CREATE TABLE devbrain.issues (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID REFERENCES devbrain.projects(id),
    title           VARCHAR(500) NOT NULL,
    category        VARCHAR(100),
    description     TEXT NOT NULL,
    root_cause      TEXT,
    fix_applied     TEXT,
    prevention      TEXT,
    files_involved  JSONB DEFAULT '[]',
    session_id      UUID REFERENCES devbrain.raw_sessions(id),
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_issues_project ON devbrain.issues(project_id);
CREATE INDEX idx_issues_category ON devbrain.issues(category);

-- ─── Codebase Index ──────────────────────────────────────────────────────────

CREATE TABLE devbrain.codebase_index (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID REFERENCES devbrain.projects(id) NOT NULL,
    file_path       TEXT NOT NULL,
    file_type       VARCHAR(20),
    summary         TEXT,
    imports         JSONB DEFAULT '[]',
    exports         JSONB DEFAULT '[]',
    embedding       vector(1024),
    last_commit     VARCHAR(40),
    last_indexed    TIMESTAMPTZ DEFAULT now(),
    UNIQUE(project_id, file_path)
);

CREATE INDEX idx_codebase_project ON devbrain.codebase_index(project_id);

-- ─── Dev Factory: Jobs ───────────────────────────────────────────────────────

CREATE TABLE devbrain.factory_jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID REFERENCES devbrain.projects(id) NOT NULL,
    title           VARCHAR(500) NOT NULL,
    description     TEXT,
    spec            TEXT,
    status          VARCHAR(50) DEFAULT 'queued',
    priority        INT DEFAULT 0,
    branch_name     VARCHAR(255),
    current_phase   VARCHAR(50),
    error_count     INT DEFAULT 0,
    max_retries     INT DEFAULT 3,
    assigned_cli    VARCHAR(50),
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_factory_jobs_project ON devbrain.factory_jobs(project_id);
CREATE INDEX idx_factory_jobs_status ON devbrain.factory_jobs(status);

-- ─── Dev Factory: Artifacts ──────────────────────────────────────────────────

CREATE TABLE devbrain.factory_artifacts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID REFERENCES devbrain.factory_jobs(id) NOT NULL,
    phase           VARCHAR(50) NOT NULL,
    artifact_type   VARCHAR(50) NOT NULL,
    content         TEXT NOT NULL,
    model_used      VARCHAR(100),
    status          VARCHAR(50) DEFAULT 'created',
    findings_count  INT DEFAULT 0,
    blocking_count  INT DEFAULT 0,
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_factory_artifacts_job ON devbrain.factory_artifacts(job_id);
CREATE INDEX idx_factory_artifacts_phase ON devbrain.factory_artifacts(phase);

-- ─── Seed Projects ───────────────────────────────────────────────────────────

INSERT INTO devbrain.projects (slug, name, root_path, description, constraints, tech_stack, lint_commands, test_commands) VALUES
(
    'brightbot',
    'BrightBot',
    '/Users/patrickkelly/Developer/lighthouse/brightbot',
    'Healthcare operations platform for Lighthouse Therapy',
    '["HIPAA compliant — no PHI in logs or error messages", "FERPA compliant — student data protection"]',
    '{"backend": "Python, FastAPI, Agno, PostgreSQL", "frontend": "Next.js, TypeScript, Tailwind", "deploy": "GCP Cloud Run"}',
    '{"python": "ruff check .", "python_format": "ruff format --check .", "frontend_lint": "pnpm -C agent-ui run lint --max-warnings 40", "frontend_types": "pnpm -C agent-ui run typecheck"}',
    '{"backend": "pytest tests/ -v --tb=short -m \"not integration\"", "frontend": "pnpm -C agent-ui exec vitest run"}'
),
(
    'pkrelay',
    'PKRelay Chrome Extension',
    '/Users/patrickkelly/pkrelay',
    'Token-efficient browser relay for AI agent interaction',
    '[]',
    '{"runtime": "Chrome Extension MV3", "language": "JavaScript"}',
    '{}',
    '{}'
),
(
    'devbrain',
    'DevBrain',
    '/Users/patrickkelly/devbrain',
    'Universal persistent memory and dev factory infrastructure',
    '[]',
    '{"mcp_server": "TypeScript, Node.js", "ingest": "Python", "db": "PostgreSQL + pgvector", "embedding": "Ollama mxbai-embed-large", "summarization": "Ollama qwen2.5:7b"}',
    '{}',
    '{}'
),
(
    'lht-vps',
    'LHT VPS Infrastructure',
    NULL,
    'n8n, Traefik, Docker on Hostinger VPS (SSH: lht-vps)',
    '[]',
    '{"containers": "Docker Compose, Traefik, n8n, PostgreSQL", "access": "SSH root@72.60.64.155"}',
    '{}',
    '{}'
);
