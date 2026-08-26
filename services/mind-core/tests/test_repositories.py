"""Phase 3 repository tests for mind-core.

These tests use the shared ``db`` fixture from ``conftest.py``, which connects
exclusively to ``mind_test_db`` via ``TEST_DATABASE_URL`` and rolls back every
transaction after the test. No persistent state is left behind, and the
application database (``mind_db``) is never touched.
"""
from __future__ import annotations

import pytest

from app.repositories import (
    AgentRepository,
    AuditRepository,
    EventRepository,
    JobRepository,
    MemoryRepository,
    PermissionRepository,
    TaskRepository,
    ToolRepository,
)


@pytest.fixture
def agents(db):
    return AgentRepository(db)


@pytest.fixture
def tasks(db):
    return TaskRepository(db)


@pytest.fixture
def jobs(db):
    return JobRepository(db)


@pytest.fixture
def events(db):
    return EventRepository(db)


@pytest.fixture
def memories(db):
    return MemoryRepository(db)


@pytest.fixture
def tools(db):
    return ToolRepository(db)


@pytest.fixture
def permissions(db):
    return PermissionRepository(db)


@pytest.fixture
def audit(db):
    return AuditRepository(db)


# ── 1. agent creation and retrieval ───────────────────────
def test_agent_create_and_get(agents):
    created = agents.create(owner="alice", purpose="test")
    assert created["id"] is not None
    assert created["status"] == "active"
    assert created["autonomy_level"] == 0

    fetched = agents.get(created["id"])
    assert fetched is not None
    assert fetched["owner"] == "alice"
    assert fetched["purpose"] == "test"


def test_agent_get_not_found(agents):
    assert agents.get("00000000-0000-0000-0000-000000000000") is None


# ── 2. agent status update ────────────────────────────────
def test_agent_update_status(agents):
    created = agents.create(owner="bob", purpose="p")
    agents.update_status(created["id"], "suspended")
    assert agents.get(created["id"])["status"] == "suspended"


def test_agent_update_autonomy_level(agents):
    created = agents.create(owner="carol", purpose="p", autonomy_level=1)
    agents.update_autonomy_level(created["id"], 2)
    assert agents.get(created["id"])["autonomy_level"] == 2


def test_agent_list_by_owner(agents):
    agents.create(owner="owner1", purpose="p")
    agents.create(owner="owner1", purpose="p2")
    agents.create(owner="owner2", purpose="p3")
    rows = agents.list_by_owner("owner1")
    assert len(rows) == 2


def test_agent_list_by_role(agents, permissions):
    role = permissions.create_role("operator")
    agents.create(owner="o", purpose="p", primary_role_id=role["id"])
    rows = agents.list_by_role(role["id"])
    assert len(rows) == 1


# ── 3. task creation and retrieval ────────────────────────
def test_task_create_and_get(tasks, agents):
    agent = agents.create(owner="o", purpose="p")
    created = tasks.create(agent_id=agent["id"], title="t1")
    assert created["id"] is not None
    assert created["status"] == "pending"
    assert tasks.get(created["id"])["title"] == "t1"


def test_task_get_not_found(tasks):
    assert tasks.get("00000000-0000-0000-0000-000000000000") is None


# ── 4. task assignment ────────────────────────────────────
def test_task_assign_agent(tasks, agents):
    agent = agents.create(owner="o", purpose="p")
    task = tasks.create(agent_id=None, title="t")
    tasks.assign_agent(task["id"], agent["id"])
    assert tasks.get(task["id"])["agent_id"] == agent["id"]


# ── 5. task status update ─────────────────────────────────
def test_task_update_status_completed_sets_completed_at(tasks, agents):
    agent = agents.create(owner="o", purpose="p")
    task = tasks.create(agent_id=agent["id"], title="t")
    tasks.update_status(task["id"], "completed")
    row = tasks.get(task["id"])
    assert row["status"] == "completed"
    assert row["completed_at"] is not None


def test_task_update_status_non_completed(tasks, agents):
    agent = agents.create(owner="o", purpose="p")
    task = tasks.create(agent_id=agent["id"], title="t")
    tasks.update_status(task["id"], "running")
    row = tasks.get(task["id"])
    assert row["status"] == "running"
    assert row["completed_at"] is None


# ── 6. task child listing ─────────────────────────────────
def test_task_child_listing(tasks, agents):
    agent = agents.create(owner="o", purpose="p")
    parent = tasks.create(agent_id=agent["id"], title="parent")
    tasks.create(agent_id=agent["id"], title="c1", parent_task_id=parent["id"])
    tasks.create(agent_id=agent["id"], title="c2", parent_task_id=parent["id"])
    children = tasks.list_children(parent["id"])
    assert len(children) == 2


def test_task_subtree(tasks, agents):
    agent = agents.create(owner="o", purpose="p")
    root = tasks.create(agent_id=agent["id"], title="root")
    mid = tasks.create(
        agent_id=agent["id"], title="mid", parent_task_id=root["id"]
    )
    tasks.create(agent_id=agent["id"], title="leaf", parent_task_id=mid["id"])
    subtree = tasks.list_subtree(root["id"])
    assert len(subtree) == 3


def test_task_list_by_status(tasks, agents):
    agent = agents.create(owner="o", purpose="p")
    tasks.create(agent_id=agent["id"], title="t1", status="running")
    tasks.create(agent_id=agent["id"], title="t2", status="running")
    tasks.create(agent_id=agent["id"], title="t3", status="pending")
    assert len(tasks.list_by_status("running")) == 2


def test_task_list_by_agent(tasks, agents):
    agent = agents.create(owner="o", purpose="p")
    tasks.create(agent_id=agent["id"], title="t1")
    tasks.create(agent_id=agent["id"], title="t2")
    assert len(tasks.list_by_agent(agent["id"])) == 2


# ── 7. job lifecycle ─────────────────────────────────────
def test_job_lifecycle(jobs, tasks, agents, tools):
    agent = agents.create(owner="o", purpose="p")
    task = tasks.create(agent_id=agent["id"], title="t")
    tool = tools.create(name="tool-a")
    job = jobs.create(task_id=task["id"], agent_id=agent["id"], tool_id=tool["id"])
    assert job["status"] == "queued"

    jobs.mark_started(job["id"])
    assert jobs.get(job["id"])["status"] == "running"

    jobs.mark_finished(job["id"], result={"ok": True})
    finished = jobs.get(job["id"])
    assert finished["status"] == "succeeded"
    assert finished["result"] == {"ok": True}

    jobs.increment_retry(job["id"])
    assert jobs.get(job["id"])["retry_count"] == 1

    jobs.mark_failed(job["id"], error="boom")
    failed = jobs.get(job["id"])
    assert failed["status"] == "failed"
    assert failed["error"] == "boom"


def test_job_list_by_task(jobs, tasks, agents):
    agent = agents.create(owner="o", purpose="p")
    task = tasks.create(agent_id=agent["id"], title="t")
    jobs.create(task_id=task["id"], agent_id=agent["id"])
    jobs.create(task_id=task["id"], agent_id=agent["id"])
    assert len(jobs.list_by_task(task["id"])) == 2


# ── 8. event append/retrieval ─────────────────────────────
def test_event_append_and_get(events, agents):
    agent = agents.create(owner="o", purpose="p")
    ev = events.append(
        type="task.created",
        producer_agent_id=agent["id"],
        correlation_id=agent["id"],
        payload={"k": "v"},
        is_security_event=True,
    )
    assert ev["id"] is not None
    assert events.get(ev["id"])["type"] == "task.created"


def test_event_list_by_correlation(events, agents):
    agent = agents.create(owner="o", purpose="p")
    events.append(
        type="a", producer_agent_id=agent["id"], correlation_id=agent["id"],
        payload={},
    )
    events.append(
        type="b", producer_agent_id=agent["id"], correlation_id=agent["id"],
        payload={},
    )
    assert len(events.list_by_correlation(agent["id"])) == 2


def test_event_list_security_events(events, agents):
    agent = agents.create(owner="o", purpose="p")
    events.append(
        type="a", producer_agent_id=agent["id"], correlation_id=agent["id"],
        payload={}, is_security_event=True,
    )
    events.append(
        type="b", producer_agent_id=agent["id"], correlation_id=agent["id"],
        payload={}, is_security_event=False,
    )
    assert len(events.list_security_events()) == 1


# ── 9. memory creation/retrieval ──────────────────────────
def test_memory_create_and_get(memories, agents):
    agent = agents.create(owner="o", purpose="p")
    mem = memories.create(
        agent_id=agent["id"], type="semantic", classification="secret",
        content="hidden", embedding=[0.1, 0.2],
    )
    fetched = memories.get(mem["id"])
    assert fetched is not None
    assert fetched["classification"] == "secret"
    assert fetched["content"] == "hidden"
    assert fetched["embedding"] == [0.1, 0.2]


# ── 10. memory soft deletion ──────────────────────────────
def test_memory_soft_delete(memories, agents):
    agent = agents.create(owner="o", purpose="p")
    mem = memories.create(agent_id=agent["id"], type="episodic", content="c")
    memories.soft_delete(mem["id"])
    # Default read excludes soft-deleted rows.
    assert memories.get(mem["id"]) is None
    # list_by_agent also excludes them.
    assert memories.list_by_agent(agent["id"]) == []

def test_memory_list_by_agent(memories, agents):
    agent = agents.create(owner="o", purpose="p")
    memories.create(agent_id=agent["id"], type="working", content="c1")
    memories.create(agent_id=agent["id"], type="working", content="c2")
    assert len(memories.list_by_agent(agent["id"])) == 2


# ── 11. tool creation/retrieval ───────────────────────────
def test_tool_create_and_get(tools, permissions):
    permissions.create_capability("shell")
    tool = tools.create(
        name="rm", requires_capability="shell", is_destructive=True,
        config={"path": "/tmp"},
    )
    fetched = tools.get(tool["id"])
    assert fetched["name"] == "rm"
    assert fetched["requires_capability"] == "shell"
    assert fetched["is_destructive"] is True

    by_name = tools.get_by_name("rm")
    assert by_name["id"] == tool["id"]


def test_tool_mark_verified_and_status(tools):
    tool = tools.create(name="safe")
    tools.mark_verified(tool["id"], True)
    assert tools.get(tool["id"])["is_verified"] is True
    tools.update_status(tool["id"], "disabled")
    assert tools.get(tool["id"])["status"] == "disabled"


def test_tool_list_destructive(tools, permissions):
    permissions.create_capability("shell")
    tools.create(name="rm", requires_capability="shell", is_destructive=True)
    tools.create(name="ls", requires_capability="shell", is_destructive=False)
    assert len(tools.list_destructive()) == 1


def test_tool_list_by_capability(tools, permissions):
    permissions.create_capability("shell")
    permissions.create_capability("network")
    tools.create(name="rm", requires_capability="shell")
    tools.create(name="ls", requires_capability="shell")
    tools.create(name="net", requires_capability="network")
    assert len(tools.list_by_capability("shell")) == 2


# ── 12. permission role/capability relationship ───────────
def test_permission_role_capability(permissions):
    role = permissions.create_role("admin")
    cap = permissions.create_capability("admin:roles")
    permissions.assign_capability_to_role(role["id"], cap["id"])
    assert permissions.get_role(role["id"])["name"] == "admin"
    assert permissions.get_capability(cap["id"])["key"] == "admin:roles"

    permissions.revoke_capability_from_role(role["id"], cap["id"])
    # Re-assigning after revoke must succeed (no leftover row).
    permissions.assign_capability_to_role(role["id"], cap["id"])


# ── 13. permission grant/revoke ───────────────────────────
def test_permission_grant_and_revoke(permissions, agents):
    agent = agents.create(owner="o", purpose="p")
    cap = permissions.create_capability("read")
    grant = permissions.grant(agent_id=agent["id"], capability_id=cap["id"])
    assert grant["revoked_at"] is None

    # Effective capabilities include the direct grant.
    eff = permissions.list_effective_capabilities(agent["id"])
    assert any(c["key"] == "read" for c in eff)

    permissions.revoke(grant["id"])
    # After revocation the grant is retained but marked revoked.
    grants = permissions.list_grants_for_agent(agent["id"])
    assert grants[0]["revoked_at"] is not None
    # No longer effective.
    eff_after = permissions.list_effective_capabilities(agent["id"])
    assert all(c["key"] != "read" for c in eff_after)


def test_permission_effective_via_role(permissions, agents):
    role = permissions.create_role("reader")
    cap = permissions.create_capability("read")
    permissions.assign_capability_to_role(role["id"], cap["id"])
    agent = agents.create(owner="o", purpose="p", primary_role_id=role["id"])
    eff = permissions.list_effective_capabilities(agent["id"])
    assert any(c["key"] == "read" and c["source"] == "role" for c in eff)


# ── 14. audit append/retrieval ────────────────────────────
def test_audit_append_and_get(audit):
    rec = audit.append(
        actor_type="agent", actor_id="agent-1", action="tool.invoke",
        outcome="success", target_type="tool", target_id="tool-1",
        capability_used="shell", context={"x": 1},
    )
    assert rec["id"] is not None
    assert audit.get(rec["id"])["outcome"] == "success"


def test_audit_list_by_actor(audit):
    audit.append(actor_type="agent", actor_id="a1", action="x", outcome="success")
    audit.append(actor_type="agent", actor_id="a1", action="y", outcome="denied")
    audit.append(actor_type="human", actor_id="h1", action="z", outcome="success")
    assert len(audit.list_by_actor("agent", "a1")) == 2


def test_audit_list_by_outcome(audit):
    audit.append(actor_type="agent", actor_id="a1", action="x", outcome="denied")
    audit.append(actor_type="agent", actor_id="a2", action="y", outcome="denied")
    audit.append(actor_type="agent", actor_id="a3", action="z", outcome="success")
    assert len(audit.list_by_outcome("denied")) == 2


# ── 15. pagination ────────────────────────────────────────
def test_pagination_tasks(tasks, agents):
    agent = agents.create(owner="o", purpose="p")
    for i in range(5):
        tasks.create(agent_id=agent["id"], title=f"t{i}")
    page = tasks.list_by_agent(agent["id"], limit=2, offset=0)
    assert len(page) == 2
    page2 = tasks.list_by_agent(agent["id"], limit=2, offset=2)
    assert len(page2) == 2


def test_pagination_agents(agents):
    for i in range(5):
        agents.create(owner="pag-owner", purpose=f"p{i}")
    first = agents.list_by_owner("pag-owner", limit=3, offset=0)
    second = agents.list_by_owner("pag-owner", limit=3, offset=3)
    assert len(first) == 3
    assert len(second) == 2


# ── 16. not-found behavior across repositories ────────────
def test_not_found_behavior(
    agents, tasks, jobs, events, memories, tools, permissions, audit
):
    missing = "00000000-0000-0000-0000-000000000000"
    assert agents.get(missing) is None
    assert tasks.get(missing) is None
    assert jobs.get(missing) is None
    assert events.get(missing) is None
    assert memories.get(missing) is None
    assert tools.get(missing) is None
    assert tools.get_by_name("does-not-exist") is None
    assert permissions.get_role(missing) is None
    assert permissions.get_capability(missing) is None
    assert audit.get(999999) is None