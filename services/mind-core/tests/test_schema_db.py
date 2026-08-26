"""Live schema verification against the isolated mind_test_db database.

These tests run only when ``TEST_DATABASE_URL`` points at ``mind_test_db``.
They never connect to ``mind_db``, never run migrations, and never persist
data (each test runs inside a transaction that is rolled back by the
``db`` fixture in ``conftest.py``).

Coverage:
    A. all 11 application tables exist
    B. required columns exist
    C. tasks.status CHECK rejects invalid status
    D. agents.autonomy_level CHECK rejects values outside 0..2
    E. permission_grants.revoked_at CHECK rejects revoked_at < granted_at
    F. memories.classification CHECK rejects invalid classifications
    G. foreign-key integrity is enforced across all 7 domains
    H. remaining CHECK constraints (agent/tool/job status, memory type,
       audit actor_type/outcome) are enforced
"""
from __future__ import annotations

import psycopg
import pytest

# The 11 application tables (the _schema_migrations bookkeeping table is
# intentionally excluded from this list).
REQUIRED_TABLES = [
    "roles",
    "capabilities",
    "role_capabilities",
    "agents",
    "permission_grants",
    "tasks",
    "tools",
    "jobs",
    "events",
    "memories",
    "audit_logs",
]

REQUIRED_COLUMNS = {
    "roles": {"id", "name", "description", "created_at"},
    "capabilities": {"id", "key", "description"},
    "role_capabilities": {"role_id", "capability_id"},
    "agents": {
        "id", "owner", "purpose", "autonomy_level", "primary_role_id",
        "status", "created_at", "updated_at",
    },
    "permission_grants": {
        "id", "agent_id", "capability_id", "scope", "granted_by",
        "granted_at", "expires_at", "revoked_at",
    },
    "tasks": {
        "id", "parent_task_id", "agent_id", "title", "status", "priority",
        "input_payload", "metadata", "created_at", "updated_at", "completed_at",
    },
    "tools": {
        "id", "name", "description", "version", "requires_capability",
        "is_destructive", "is_verified", "config", "status", "created_at",
    },
    "jobs": {
        "id", "task_id", "agent_id", "tool_id", "status", "scheduled_at",
        "started_at", "finished_at", "result", "error", "retry_count",
    },
    "events": {
        "id", "type", "producer_agent_id", "correlation_id", "payload",
        "signature", "is_security_event", "created_at",
    },
    "memories": {
        "id", "agent_id", "type", "classification", "content", "embedding",
        "metadata", "retention_until", "created_at", "updated_at", "deleted_at",
    },
    "audit_logs": {
        "id", "actor_type", "actor_id", "action", "target_type", "target_id",
        "capability_used", "outcome", "context", "created_at",
    },
}


def _columns(conn, table: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s AND table_schema = 'public'",
            (table,),
        )
        return {r[0] for r in cur.fetchall()}


# ── A. tables exist ─────────────────────────────────────────────

def test_all_application_tables_exist(db):
    with db.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
        tables = {r[0] for r in cur.fetchall()}
    missing = [t for t in REQUIRED_TABLES if t not in tables]
    assert not missing, f"Missing application tables: {missing}"


# ── B. columns exist ────────────────────────────────────────────

def test_required_columns_exist(db):
    errors = []
    for table, cols in REQUIRED_COLUMNS.items():
        missing = cols - _columns(db, table)
        if missing:
            errors.append(f"{table}: missing {sorted(missing)}")
    assert not errors, "\n".join(errors)


# ── C. task status CHECK ────────────────────────────────────────

def test_task_status_rejects_invalid(db):
    with db.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO tasks (title, status) VALUES ('x', 'nonsense')"
        )


# ── D. autonomy_level CHECK ─────────────────────────────────────

def test_autonomy_level_rejects_out_of_range(db):
    with db.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO agents (owner, purpose, autonomy_level) "
            "VALUES ('o', 'p', 9)"
        )


# ── E. permission_grants revocation ordering CHECK ─────────────

def test_grant_revocation_order_rejects_invalid(db):
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents (owner, purpose) VALUES ('o', 'p') RETURNING id")
        agent_id = cur.fetchone()[0]
        cur.execute("INSERT INTO capabilities (key) VALUES ('read') RETURNING id")
        cap_id = cur.fetchone()[0]
    with db.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO permission_grants "
            "(agent_id, capability_id, granted_at, revoked_at) "
            "VALUES (%s, %s, now(), now() - interval '1 hour')",
            (agent_id, cap_id),
        )


# ── F. memory classification CHECK ──────────────────────────────

def test_memory_classification_rejects_invalid(db):
    with db.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO memories (type, classification) "
            "VALUES ('episodic', 'topsecret')"
        )


# ── G. foreign-key integrity ────────────────────────────────────

def test_tasks_agent_fk_enforced(db):
    """A task cannot reference a non-existent agent (FK to agents.id)."""
    with db.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO tasks (title, agent_id) "
            "VALUES ('orphan', '00000000-0000-0000-0000-000000000000')"
        )


def test_tools_capability_fk_enforced(db):
    """A tool's requires_capability must match an existing capability key."""
    with db.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO tools (name, requires_capability) "
            "VALUES ('t', 'no-such-capability')"
        )


def test_jobs_task_fk_enforced(db):
    """A job cannot reference a non-existent task (FK to tasks.id)."""
    with db.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO jobs (task_id) "
            "VALUES ('00000000-0000-0000-0000-000000000000')"
        )


def test_jobs_tool_fk_enforced(db):
    """A job cannot reference a non-existent tool (FK to tools.id)."""
    with db.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO jobs (tool_id) "
            "VALUES ('00000000-0000-0000-0000-000000000000')"
        )


def test_events_agent_fk_enforced(db):
    """An event cannot reference a non-existent producer agent."""
    with db.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO events (type, producer_agent_id, payload) "
            "VALUES ('x', '00000000-0000-0000-0000-000000000000', '{}')"
        )


def test_memories_agent_fk_enforced(db):
    """A memory cannot reference a non-existent agent."""
    with db.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO memories (agent_id, type, classification) "
            "VALUES ('00000000-0000-0000-0000-000000000000', 'episodic', 'internal')"
        )


def test_role_capabilities_fk_enforced(db):
    """role_capabilities rejects unknown role/capability ids."""
    with db.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO role_capabilities (role_id, capability_id) "
            "VALUES ('00000000-0000-0000-0000-000000000000', "
            "'00000000-0000-0000-0000-000000000000')"
        )


def test_permission_grants_fk_enforced(db):
    """permission_grants rejects unknown agent/capability ids."""
    with db.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO permission_grants (agent_id, capability_id) "
            "VALUES ('00000000-0000-0000-0000-000000000000', "
            "'00000000-0000-0000-0000-000000000000')"
        )


# ── H. remaining CHECK constraints ─────────────────────────────

def test_agent_status_rejects_invalid(db):
    with db.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO agents (owner, purpose, status) "
            "VALUES ('o', 'p', 'frozen')"
        )


def test_tool_status_rejects_invalid(db):
    with db.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO tools (name, status) VALUES ('t', 'bogus')"
        )


def test_job_status_rejects_invalid(db):
    with db.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO jobs (status) VALUES ('exploded')"
        )


def test_memory_type_rejects_invalid(db):
    with db.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO memories (type, classification) "
            "VALUES ('procedural', 'internal')"
        )


def test_audit_actor_type_rejects_invalid(db):
    with db.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO audit_logs (actor_type, actor_id, action, outcome) "
            "VALUES ('daemon', 'a', 'act', 'success')"
        )


def test_audit_outcome_rejects_invalid(db):
    with db.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO audit_logs (actor_type, actor_id, action, outcome) "
            "VALUES ('agent', 'a', 'act', 'maybe')"
        )