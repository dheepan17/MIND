"""Static schema verification for the M.I.N.D. PostgreSQL database.

Pure parse of the migration SQL. Runs with no database and no live
connection, so it always executes in CI. Live (DB-backed) verification
lives in ``tests/test_schema_db.py`` and runs only against the isolated
``mind_test_db`` database via the ``db`` fixture in ``conftest.py``.
"""
from __future__ import annotations

import re
from pathlib import Path

MIGRATION_FILE = (
    Path(__file__).resolve().parents[1] / "migrations" / "0001_init.sql"
)

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

# table -> set of required columns
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


def _read_sql() -> str:
    return MIGRATION_FILE.read_text(encoding="utf-8")


def _strip_comments(sql: str) -> str:
    """Remove -- line comments and /* */ block comments."""
    sql = re.sub(r"--[^\n]*", "", sql)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    return sql


def _create_table_blocks(sql: str) -> dict[str, str]:
    """Map table name -> column-definition body for each CREATE TABLE."""
    sql = _strip_comments(sql)
    blocks: dict[str, str] = {}
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([\"\w]+)\s*\((.*?)\);",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(sql):
        name = match.group(1).strip().strip('"')
        blocks[name] = match.group(2)
    return blocks


def test_migration_file_exists():
    assert MIGRATION_FILE.exists(), f"Missing migration file: {MIGRATION_FILE}"


def test_required_tables_present():
    blocks = _create_table_blocks(_read_sql())
    missing = [t for t in REQUIRED_TABLES if t not in blocks]
    assert not missing, f"Missing tables: {missing}"


def test_required_columns_present():
    blocks = _create_table_blocks(_read_sql())
    errors = []
    for table, cols in REQUIRED_COLUMNS.items():
        body = blocks.get(table, "")
        present = set()
        for line in body.split(","):
            m = re.match(r"\s*([\"\w]+)", line)
            if m:
                present.add(m.group(1).strip('"'))
        missing = cols - present
        if missing:
            errors.append(f"{table}: missing {sorted(missing)}")
    assert not errors, "\n".join(errors)


def test_check_constraints_present():
    """Every CHECK constraint across the 7 domains is declared in SQL."""
    sql = _read_sql()
    # Permission layer
    assert "autonomy_level BETWEEN 0 AND 2" in sql
    assert "status IN ('active', 'suspended', 'retired')" in sql
    assert "revoked_at IS NULL OR revoked_at >= granted_at" in sql
    # Work layer — tasks
    assert (
        "status IN ('pending', 'running', 'completed', 'failed', 'blocked', 'cancelled')"
        in sql
    )
    # Work layer — tools
    assert "status IN ('active', 'disabled')" in sql
    # Work layer — jobs
    assert (
        "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')" in sql
    )
    # Memory store
    assert "type IN ('episodic', 'semantic', 'working')" in sql
    assert "classification IN ('public', 'internal', 'secret')" in sql
    # Audit log
    assert "actor_type IN ('agent', 'human', 'role')" in sql
    assert "outcome IN ('success', 'denied', 'error')" in sql


def test_foreign_key_references_present():
    """Every cross-entity relationship is declared in the migration SQL.

    We assert the presence of each ``REFERENCES <target>`` target rather
    than an exact column layout, so the test stays robust to whitespace
    and ``ON DELETE`` clause formatting changes.
    """
    sql = _read_sql()
    expected_refs = [
        # permission layer
        "REFERENCES roles(id)",            # role_capabilities.role_id
        "REFERENCES roles(id)",            # agents.primary_role_id
        "REFERENCES capabilities(id)",     # role_capabilities.capability_id
        "REFERENCES agents(id)",           # permission_grants.agent_id
        "REFERENCES capabilities(id)",     # permission_grants.capability_id
        "REFERENCES agents(id)",           # permission_grants.granted_by
        # work layer
        "REFERENCES tasks(id)",            # tasks.parent_task_id (self-ref)
        "REFERENCES agents(id)",           # tasks.agent_id
        "REFERENCES capabilities(key)",    # tools.requires_capability
        "REFERENCES tasks(id)",            # jobs.task_id
        "REFERENCES agents(id)",           # jobs.agent_id
        "REFERENCES tools(id)",            # jobs.tool_id
        # event bus
        "REFERENCES agents(id)",           # events.producer_agent_id
        # memory store
        "REFERENCES agents(id)",           # memories.agent_id
    ]
    missing = [ref for ref in expected_refs if ref not in sql]
    assert not missing, f"Missing FK references: {missing}"
