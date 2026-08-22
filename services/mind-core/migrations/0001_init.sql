-- Migration 0001: Initial M.I.N.D. PostgreSQL schema
--
-- Entities:
--   Permission layer : roles, capabilities, role_capabilities,
--                      agents, permission_grants
--   Work layer       : tasks, tools, jobs
--   Event bus        : events  (signed; mitigates threat T4)
--   Memory store     : memories (soft delete for retention/deletion)
--   Audit            : audit_logs (append-only, redacted context)
--
-- Apply with:  DATABASE_URL=... python -m migrations.run
-- Verify with: pytest tests/test_schema.py

CREATE EXTENSION IF NOT EXISTS "pgcrypto";  -- provides gen_random_uuid()

-- ──────────────────────────────────────────────────────────────
-- Permission layer
-- ──────────────────────────────────────────────────────────────

CREATE TABLE roles (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT UNIQUE NOT NULL,            -- guest | operator | admin
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE capabilities (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key         TEXT UNIQUE NOT NULL,            -- read|write|shell|network|data:pii|admin:roles
    description TEXT
);

-- M:N mapping of roles to capabilities (built-in grants).
CREATE TABLE role_capabilities (
    role_id       UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    capability_id UUID NOT NULL REFERENCES capabilities(id) ON DELETE CASCADE,
    PRIMARY KEY (role_id, capability_id)
);

CREATE TABLE agents (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner          TEXT NOT NULL,
    purpose        TEXT NOT NULL,
    autonomy_level SMALLINT NOT NULL DEFAULT 0 CHECK (autonomy_level BETWEEN 0 AND 2),
    primary_role_id UUID REFERENCES roles(id) ON DELETE SET NULL,
    status         TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'suspended', 'retired')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Scoped, expiring grants (deny-by-default; immediate revocation).
-- These are per-agent overrides layered on top of the primary role.
CREATE TABLE permission_grants (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id      UUID NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    capability_id UUID NOT NULL REFERENCES capabilities(id) ON DELETE CASCADE,
    scope         TEXT NOT NULL DEFAULT '*',
    granted_by    UUID REFERENCES agents(id) ON DELETE SET NULL,
    granted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ,
    CHECK (revoked_at IS NULL OR revoked_at >= granted_at)
);

CREATE INDEX idx_grants_agent ON permission_grants (agent_id, revoked_at);

-- ──────────────────────────────────────────────────────────────
-- Work layer
-- ──────────────────────────────────────────────────────────────

-- Tasks form a self-referential tree (planner decomposition).
CREATE TABLE tasks (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    parent_task_id UUID REFERENCES tasks(id) ON DELETE CASCADE,
    agent_id       UUID REFERENCES agents(id) ON DELETE SET NULL,
    title          TEXT NOT NULL,
    description    TEXT,
    status         TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'running', 'completed', 'failed', 'blocked', 'cancelled')),
    priority       SMALLINT DEFAULT 5,
    input_payload  JSONB,
    metadata       JSONB,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at   TIMESTAMPTZ
);

CREATE INDEX idx_tasks_parent ON tasks (parent_task_id);
CREATE INDEX idx_tasks_status ON tasks (status);

-- Tool registry. Explicit-allow: a tool is denied unless a capability grants it.
CREATE TABLE tools (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                TEXT UNIQUE NOT NULL,
    description         TEXT,
    version             TEXT,
    requires_capability TEXT REFERENCES capabilities(key) ON DELETE SET NULL,
    is_destructive      BOOLEAN NOT NULL DEFAULT false,
    is_verified         BOOLEAN NOT NULL DEFAULT false,   -- unsigned tools blocked
    config              JSONB,
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'disabled')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Async execution records (jobs run tasks and/or invoke tools).
CREATE TABLE jobs (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id      UUID REFERENCES tasks(id) ON DELETE SET NULL,
    agent_id     UUID REFERENCES agents(id) ON DELETE SET NULL,
    tool_id      UUID REFERENCES tools(id) ON DELETE SET NULL,
    status       TEXT NOT NULL DEFAULT 'queued'
                   CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    scheduled_at TIMESTAMPTZ,
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    result       JSONB,
    error        TEXT,
    retry_count  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_jobs_task ON jobs (task_id);
CREATE INDEX idx_jobs_status ON jobs (status);

-- ──────────────────────────────────────────────────────────────
-- Event bus (signed events; mitigates threat T4)
-- ──────────────────────────────────────────────────────────────

CREATE TABLE events (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type              TEXT NOT NULL,
    producer_agent_id UUID REFERENCES agents(id) ON DELETE SET NULL,
    correlation_id    UUID,                  -- links to a task/job UUID
    payload           JSONB NOT NULL,
    signature         TEXT,                  -- NULL => unsigned (internal only)
    is_security_event BOOLEAN NOT NULL DEFAULT false,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_events_correlation ON events (correlation_id);
CREATE INDEX idx_events_security ON events (is_security_event, created_at);

-- ──────────────────────────────────────────────────────────────
-- Memory store (soft delete honors retention / deletion requests)
-- ──────────────────────────────────────────────────────────────

CREATE TABLE memories (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id        UUID REFERENCES agents(id) ON DELETE SET NULL,
    type            TEXT NOT NULL CHECK (type IN ('episodic', 'semantic', 'working')),
    classification  TEXT NOT NULL DEFAULT 'internal'
                     CHECK (classification IN ('public', 'internal', 'secret')),
    content         TEXT,
    -- Stored as NUMERIC[] for portability on vanilla PostgreSQL.
    -- Migrate to pgvector `vector(1536)` when ANN similarity search
    -- is required (extension is optional and not created here).
    embedding       NUMERIC[],
    metadata        JSONB,
    retention_until TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ                    -- soft delete
);

CREATE INDEX idx_memories_agent ON memories (agent_id, classification);
CREATE INDEX idx_memories_retention ON memories (retention_until) WHERE deleted_at IS NULL;

-- ──────────────────────────────────────────────────────────────
-- Audit log (append-only, immutable, redacted context)
-- ──────────────────────────────────────────────────────────────

CREATE TABLE audit_logs (
    id              BIGSERIAL PRIMARY KEY,
    actor_type      TEXT NOT NULL CHECK (actor_type IN ('agent', 'human', 'role')),
    actor_id        TEXT NOT NULL,
    action          TEXT NOT NULL,
    target_type     TEXT,
    target_id       TEXT,
    capability_used TEXT,
    outcome         TEXT NOT NULL CHECK (outcome IN ('success', 'denied', 'error')),
    context         JSONB,                     -- Secret fields redacted (data policy #4)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_audit_created ON audit_logs (created_at);
CREATE INDEX idx_audit_actor ON audit_logs (actor_type, actor_id);