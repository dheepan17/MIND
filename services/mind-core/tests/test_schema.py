"""Schema verification for the M.I.N.D. PostgreSQL database.

Two layers of testing:

1. ``test_*`` (static) -- pure parse of the migration SQL. Runs with no
   database and no third-party dependencies, so it always executes in CI.
2. ``test_db_*`` (live) -- applies the migration to a live PostgreSQL
   instance (DATABASE_URL) inside a rolled-back transaction and asserts
   tables, columns, constraints, and key behaviors. Skipped when
   DATABASE_URL is unset or psycopg is unavailable.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import psycopg
import pytest

MIGRATION_FILE = (
    Path(__file__).resolve().parents[1] / "migrations" / "0001_init.sql"
)

DATABASE_URL = os.environ.get("DATABASE_URL")

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


# ── Static (no-DB) tests ────────────────────────────────────────

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
    sql = _read_sql()
    assert "autonomy_level BETWEEN 0 AND 2" in sql
    assert (
        "status IN ('pending', 'running', 'completed', 'failed', 'blocked', 'cancelled')"
        in sql
    )
    assert "classification IN ('public', 'internal', 'secret')" in sql
    assert "outcome IN ('success', 'denied', 'error')" in sql
    assert "revoked_at IS NULL OR revoked_at >= granted_at" in sql


# ── Live (DB-backed) tests ──────────────────────────────────────

# Only the DB-backed tests below require a live PostgreSQL instance.
def _require_db() -> None:
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set; skipping live DB schema tests")


@pytest.fixture
def conn():
    _require_db()
    try:
        import psycopg
    except ImportError:
        pytest.skip("psycopg not installed")
    connection = psycopg.connect(DATABASE_URL, autocommit=False)
    try:
        with connection.cursor() as cur:
            cur.execute(_read_sql())
        yield connection
    finally:
        connection.rollback()
        connection.close()


def _columns(conn, table: str) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s AND table_schema = 'public'",
            (table,),
        )
        return {r[0] for r in cur.fetchall()}


def test_db_tables_created(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
        tables = {r[0] for r in cur.fetchall()}
    missing = [t for t in REQUIRED_TABLES if t not in tables]
    assert not missing, f"Missing tables in DB: {missing}"


def test_db_required_columns(conn):
    errors = []
    for table, cols in REQUIRED_COLUMNS.items():
        missing = cols - _columns(conn, table)
        if missing:
            errors.append(f"{table}: missing {sorted(missing)}")
    assert not errors, "\n".join(errors)


def test_db_rejects_bad_task_status(conn):
    with conn.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO tasks (title, status) VALUES ('x', 'nonsense')"
        )


def test_db_rejects_bad_autonomy_level(conn):
    with conn.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO agents (owner, purpose, autonomy_level) "
            "VALUES ('o', 'p', 9)"
        )


def test_db_rejects_bad_grant_revocation_order(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO agents (owner, purpose) VALUES ('o', 'p') RETURNING id")
        agent_id = cur.fetchone()[0]
        cur.execute("INSERT INTO capabilities (key) VALUES ('read') RETURNING id")
        cap_id = cur.fetchone()[0]
    with conn.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO permission_grants "
            "(agent_id, capability_id, granted_at, revoked_at) "
            "VALUES (%s, %s, now(), now() - interval '1 hour')",
            (agent_id, cap_id),
        )


def test_db_rejects_bad_memory_classification(conn):
    with conn.cursor() as cur, pytest.raises(psycopg.Error):
        cur.execute(
            "INSERT INTO memories (type, classification) VALUES ('episodic', 'topsecret')"
        )
