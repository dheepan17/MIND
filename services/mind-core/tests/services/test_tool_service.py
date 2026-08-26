"""Tests for ToolService (Phase 3 tool registry)."""
from __future__ import annotations

import pytest

from app.repositories.audit import AuditRepository
from app.repositories.permissions import PermissionRepository
from app.repositories.tools import ToolRepository
from app.services.errors import NotFoundError, ValidationError
from app.services.tool_service import ToolService


@pytest.fixture
def tools(db):
    return ToolRepository(db)


@pytest.fixture
def permissions(db):
    return PermissionRepository(db)


@pytest.fixture
def audit(db):
    return AuditRepository(db)


@pytest.fixture
def tools_service(tools, permissions, audit):
    return ToolService(tools, permissions, audit)


# ── 1. create + read ──────────────────────────────────────
def test_create_and_get(tools_service, permissions):
    permissions.create_capability("shell")
    tool = tools_service.create_tool(
        name="rm",
        requires_capability="shell",
        is_destructive=True,
        config={"path": "/tmp"},
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
def test_create_without_capability(tools_service):
    tool = tools_service.create_tool(name="safe")
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
def test_update_status(tools_service):
    tool = tools_service.create_tool(name="t")
    tools_service.update_status(tool["id"], "disabled")
    assert tools_service.get_tool(tool["id"])["status"] == "disabled"

    with pytest.raises(ValidationError):
        tools_service.update_status(tool["id"], "bogus")
    with pytest.raises(NotFoundError):
        tools_service.update_status(
            "00000000-0000-0000-0000-000000000000", "active"
        )


# ── 7. mark_verified ──────────────────────────────────────
def test_mark_verified(tools_service):
    tool = tools_service.create_tool(name="t")
    tools_service.mark_verified(tool["id"], True)
    assert tools_service.get_tool(tool["id"])["is_verified"] is True
    tools_service.mark_verified(tool["id"], False)
    assert tools_service.get_tool(tool["id"])["is_verified"] is False

    with pytest.raises(ValidationError):
        tools_service.mark_verified(tool["id"], "yes")
    with pytest.raises(NotFoundError):
        tools_service.mark_verified(
            "00000000-0000-0000-0000-000000000000", True
        )


# ── 8. list_destructive ────────────────────────────────────
def test_list_destructive(tools_service, permissions):
    permissions.create_capability("shell")
    tools_service.create_tool(name="rm", requires_capability="shell", is_destructive=True)
    tools_service.create_tool(name="ls", requires_capability="shell", is_destructive=False)
    assert len(tools_service.list_destructive()) == 1


# ── 9. list_by_capability ─────────────────────────────────
def test_list_by_capability(tools_service, permissions):
    permissions.create_capability("shell")
    permissions.create_capability("network")
    tools_service.create_tool(name="rm", requires_capability="shell")
    tools_service.create_tool(name="ls", requires_capability="shell")
    tools_service.create_tool(name="net", requires_capability="network")
    assert len(tools_service.list_by_capability("shell")) == 2
    assert len(tools_service.list_by_capability("network")) == 1


# ── 10. audit trail on mutations ──────────────────────────
def test_audit_on_create(tools_service, audit):
    tool = tools_service.create_tool(name="t")
    records = audit.list_by_target("tool", str(tool["id"]))
    assert any(r["action"] == "tool.create" for r in records)


def test_audit_on_update_status(tools_service, audit):
    tool = tools_service.create_tool(name="t")
    tools_service.update_status(tool["id"], "disabled")
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