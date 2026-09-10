"""Tests for ToolService (Phase 3 + Phase 5.9 write authorization).

The service now requires a ``psycopg.Connection`` (consistent with every other
service in the project) and gates mutations on the generic ``write``
capability. Tests that exercise successful mutations pass a committed writer
agent's id as ``actor_id``.
"""
from __future__ import annotations

import uuid

import pytest

from app.repositories.agents import AgentRepository
from app.repositories.permissions import PermissionRepository
from app.services.errors import AuthorizationError, NotFoundError, ValidationError
from app.services.tool_service import ToolService


def _make_writer_agent(db) -> str:
    """Create a committed agent with the generic ``write`` capability."""
    perms = PermissionRepository(db)
    agents = AgentRepository(db)
    role = perms.create_role(f"writer-{uuid.uuid4()}")
    cap = perms.create_capability("write")
    perms.assign_capability_to_role(role["id"], cap["id"])
    agent = agents.create(
        owner=f"owner-{uuid.uuid4()}", purpose="tool-test", primary_role_id=role["id"]
    )
    return str(agent["id"])


@pytest.fixture
def writer_agent_id(db):
    return _make_writer_agent(db)


@pytest.fixture
def tools_service(db):
    return ToolService(db)


# ── 1. create + read ──────────────────────────────────────
def test_create_and_get(tools_service, writer_agent_id):
    tool = tools_service.create_tool(
        name="rm",
        requires_capability="shell",
        is_destructive=True,
        config={"path": "/tmp"},
        actor_id=writer_agent_id,
    )
    assert tool["id"] is not None
    assert tool["name"] == "rm"
    assert tool["requires_capability"] == "shell"
    assert tool["is_destructive"] is True
    assert tool["is_verified"] is False
    assert tool["status"] == "active"

    fetched = tools_service.get_tool(tool["id"])
    assert fetched["name"] == "rm"

    by_name = tools_service.get_tool_by_name("rm")
    assert by_name["id"] == tool["id"]


# ── 2. create without capability linkage ──────────────────
def test_create_without_capability(tools_service, writer_agent_id):
    tool = tools_service.create_tool(name="safe", actor_id=writer_agent_id)
    assert tool["requires_capability"] is None
    assert tool["is_destructive"] is False


# ── 3. create rejects unknown capability key ──────────────
def test_create_rejects_unknown_capability(tools_service):
    with pytest.raises(ValidationError):
        tools_service.create_tool(name="x", requires_capability="nope")


# ── 4. create validation ──────────────────────────────────
def test_create_validation(tools_service):
    with pytest.raises(ValidationError):
        tools_service.create_tool(name="")
    with pytest.raises(ValidationError):
        tools_service.create_tool(name="x", status="weird")
    with pytest.raises(ValidationError):
        tools_service.create_tool(name="x", is_destructive="yes")
    with pytest.raises(ValidationError):
        tools_service.create_tool(name="x", config=[1, 2])


# ── 5. get not found ──────────────────────────────────────
def test_get_not_found(tools_service):
    with pytest.raises(NotFoundError):
        tools_service.get_tool("00000000-0000-0000-0000-000000000000")
    with pytest.raises(NotFoundError):
        tools_service.get_tool_by_name("does-not-exist")


def test_get_bad_uuid(tools_service):
    with pytest.raises(ValidationError):
        tools_service.get_tool("not-a-uuid")


# ── 6. update_status ──────────────────────────────────────
def test_update_status(tools_service, writer_agent_id):
    tool = tools_service.create_tool(
        name="t", actor_id=writer_agent_id
    )
    tools_service.update_status(
        tool["id"], "disabled", actor_id=writer_agent_id
    )
    assert tools_service.get_tool(tool["id"])["status"] == "disabled"

    with pytest.raises(ValidationError):
        tools_service.update_status(
            tool["id"], "bogus", actor_id=writer_agent_id
        )
    with pytest.raises(NotFoundError):
        tools_service.update_status(
            "00000000-0000-0000-0000-000000000000", "active", actor_id=writer_agent_id
        )


# ── 7. mark_verified ──────────────────────────────────────
def test_mark_verified(tools_service, writer_agent_id):
    tool = tools_service.create_tool(name="t", actor_id=writer_agent_id)
    tools_service.mark_verified(
        tool["id"], True, actor_id=writer_agent_id
    )
    assert tools_service.get_tool(tool["id"])["is_verified"] is True
    tools_service.mark_verified(
        tool["id"], False, actor_id=writer_agent_id
    )
    assert tools_service.get_tool(tool["id"])["is_verified"] is False

    with pytest.raises(ValidationError):
        tools_service.mark_verified(
            tool["id"], "yes", actor_id=writer_agent_id
        )
    with pytest.raises(NotFoundError):
        tools_service.mark_verified(
            "00000000-0000-0000-0000-000000000000", True, actor_id=writer_agent_id
        )


# ── 8. list_destructive ────────────────────────────────────
def test_list_destructive(tools_service, writer_agent_id, db):
    perms = PermissionRepository(db)
    perms.create_capability("shell")
    tools_service.create_tool(
        name="rm", requires_capability="shell", is_destructive=True, actor_id=writer_agent_id
    )
    tools_service.create_tool(
        name="ls", requires_capability="shell", is_destructive=False, actor_id=writer_agent_id
    )
    assert len(tools_service.list_destructive()) == 1


# ── 9. list_by_capability ─────────────────────────────────
def test_list_by_capability(tools_service, writer_agent_id, db):
    perms = PermissionRepository(db)
    perms.create_capability("shell")
    perms.create_capability("network")
    tools_service.create_tool(
        name="rm", requires_capability="shell", actor_id=writer_agent_id
    )
    tools_service.create_tool(
        name="ls", requires_capability="shell", actor_id=writer_agent_id
    )
    tools_service.create_tool(
        name="net", requires_capability="network", actor_id=writer_agent_id
    )
    assert len(tools_service.list_by_capability("shell")) == 2
    assert len(tools_service.list_by_capability("network")) == 1


# ── 10. audit trail on mutations ──────────────────────────
def test_audit_on_create(tools_service, writer_agent_id, db):
    tool = tools_service.create_tool(name="t", actor_id=writer_agent_id)
    audit = ToolService(db)._audit_repo
    records = audit.list_by_target("tool", str(tool["id"]))
    assert any(r["action"] == "tool.create" for r in records)


def test_audit_on_update_status(tools_service, writer_agent_id, db):
    tool = tools_service.create_tool(name="t", actor_id=writer_agent_id)
    tools_service.update_status(
        tool["id"], "disabled", actor_id=writer_agent_id
    )
    audit = ToolService(db)._audit_repo
    records = audit.list_by_target("tool", str(tool["id"]))
    assert any(r["action"] == "tool.update_status" for r in records)


# ── 11. paging validation ─────────────────────────────────
def test_paging_validation(tools_service):
    with pytest.raises(ValidationError):
        tools_service.list_destructive(limit=0)
    with pytest.raises(ValidationError):
        tools_service.list_destructive(limit=101)
    with pytest.raises(ValidationError):
        tools_service.list_destructive(offset=-1)


# ── 12. write authorization gate ──────────────────────────
def test_create_requires_write(tools_service):
    with pytest.raises(AuthorizationError):
        tools_service.create_tool(name="x", actor_id="00000000-0000-0000-0000-000000000000")


def test_update_status_requires_write(tools_service):
    with pytest.raises(AuthorizationError):
        tools_service.update_status(
            "00000000-0000-0000-0000-000000000000", "active", actor_id="00000000-0000-0000-0000-000000000000"
        )


def test_mark_verified_requires_write(tools_service):
    with pytest.raises(AuthorizationError):
        tools_service.mark_verified(
            "00000000-0000-0000-0000-000000000000", True, actor_id="00000000-0000-0000-0000-000000000000"
        )


# ── 13. denied audit record ───────────────────────────────
def test_denied_audit_on_create(tools_service, db):
    tools_service.create_tool(
        name="x", actor_id="00000000-0000-0000-0000-000000000000"
    )
    audit = ToolService(db)._audit_repo
    records = audit.list_by_target("tool", "pending")
    denied = [r for r in records if r["action"] == "tool.create" and r["outcome"] == "denied"]
    assert len(denied) >= 1
    assert denied[0]["capability_used"] == "write"
