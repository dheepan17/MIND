"""Phase 4.5 tests for TaskService.

Run against the isolated ``mind_test_db`` via the shared ``db`` fixture
(which rolls back after each test). They exercise the task business
boundary: creation, reads, status/agent mutations, hierarchy listing,
pagination, validation, NotFoundError semantics, audit integration, and
transaction ownership (the service never commits or rolls back itself).

All identifiers are unique per test so the suite is self-isolating and
never relies on a clean persistent DB or global truncation.
"""
from __future__ import annotations

import uuid

import pytest
from psycopg.pq import TransactionStatus

from app.db.transaction import transaction
from app.repositories.agents import AgentRepository
from app.repositories.tasks import TaskRepository
from app.services import (
    AuditService,
    NotFoundError,
    TaskService,
    ValidationError,
)

MISSING = "00000000-0000-0000-0000-000000000000"
ACTOR = "actor-22222222-2222-2222-2222-222222222222"


@pytest.fixture
def service(db):
    return TaskService(db)


@pytest.fixture
def agents(db):
    return AgentRepository(db)


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def _make_agent(agents, owner: str | None = None) -> dict:
    return agents.create(owner=owner or _uid("owner"), purpose="p")


def _make_task(service, agents, **overrides) -> dict:
    payload = {
        "title": _uid("task"),
        "actor_id": ACTOR,
    }
    if "agent_id" not in overrides:
        payload["agent_id"] = _make_agent(agents)["id"]
    payload.update(overrides)
    return service.create(**payload)


# ── 1. create success ───────────────────────────────────
def test_create_task(service, agents):
    agent = _make_agent(agents)
    task = service.create(
        title="do something", actor_id=ACTOR, agent_id=agent["id"]
    )
    assert task["id"] is not None
    assert task["title"] == "do something"
    assert task["status"] == "pending"
    assert task["priority"] == 5
    assert task["agent_id"] == agent["id"]


# ── 2. create validation ────────────────────────────────
def test_create_validation(service, agents):
    agent = _make_agent(agents)
    with pytest.raises(ValidationError):
        service.create(title="", actor_id=ACTOR, agent_id=agent["id"])
    with pytest.raises(ValidationError):
        service.create(title="  ", actor_id=ACTOR, agent_id=agent["id"])
    with pytest.raises(ValidationError):
        service.create(title="t", actor_id=ACTOR, status="bogus")
    with pytest.raises(ValidationError):
        service.create(title="t", actor_id=ACTOR, priority="high")
    with pytest.raises(ValidationError):
        service.create(title="t", actor_id=ACTOR, priority=True)
    with pytest.raises(ValidationError):
        service.create(title="t", actor_id=ACTOR, actor_type="daemon")
    with pytest.raises(ValidationError):
        service.create(title="t", actor_id=ACTOR, agent_id="not-a-uuid")
    with pytest.raises(ValidationError):
        service.create(title="t", actor_id=ACTOR, parent_task_id="nope")


# ── 3. create with parent ───────────────────────────────
def test_create_with_parent(service, agents):
    parent = _make_task(service, agents)
    child = service.create(
        title="child", actor_id=ACTOR, parent_task_id=parent["id"]
    )
    assert child["parent_task_id"] == parent["id"]


# ── 4. create nonexistent parent rejected ───────────────
def test_create_nonexistent_parent(service, agents):
    _make_agent(agents)
    with pytest.raises(NotFoundError):
        service.create(
            title="orphan", actor_id=ACTOR, parent_task_id=MISSING
        )


# ── 5. create nonexistent agent rejected ────────────────
def test_create_nonexistent_agent(service):
    with pytest.raises(NotFoundError):
        service.create(title="t", actor_id=ACTOR, agent_id=MISSING)


# ── 6. get success ──────────────────────────────────────
def test_get_existing_task(service, agents):
    task = _make_task(service, agents)
    fetched = service.get(task["id"])
    assert fetched["id"] == task["id"]
    assert fetched["title"] == task["title"]


# ── 7. get not found ────────────────────────────────────
def test_get_nonexistent_task(service):
    with pytest.raises(NotFoundError):
        service.get(MISSING)
    with pytest.raises(ValidationError):
        service.get("not-a-uuid")


# ── 8. update_status success ────────────────────────────
def test_update_status(service, agents):
    task = _make_task(service, agents)
    updated = service.update_status(task["id"], "running", actor_id=ACTOR)
    assert updated["status"] == "running"
    assert service.get(task["id"])["status"] == "running"


# ── 9. update_status to completed sets completed_at ─────
def test_update_status_completed_sets_timestamp(service, agents):
    task = _make_task(service, agents)
    updated = service.update_status(task["id"], "completed", actor_id=ACTOR)
    assert updated["status"] == "completed"
    assert updated["completed_at"] is not None


# ── 10. invalid status ──────────────────────────────────
def test_invalid_status(service, agents):
    task = _make_task(service, agents)
    with pytest.raises(ValidationError):
        service.update_status(task["id"], "bogus", actor_id=ACTOR)
    with pytest.raises(ValidationError):
        service.update_status("not-a-uuid", "running", actor_id=ACTOR)


# ── 11. assign_agent success ─────────────────────────────
def test_assign_agent(service, agents):
    task = _make_task(service, agents)
    new_agent = _make_agent(agents)
    updated = service.assign_agent(
        task["id"], new_agent["id"], actor_id=ACTOR
    )
    assert updated["agent_id"] == new_agent["id"]
    assert service.get(task["id"])["agent_id"] == new_agent["id"]


# ── 12. assign_agent unassign (None) ────────────────────
def test_assign_agent_unassign(service, agents):
    task = _make_task(service, agents)
    updated = service.assign_agent(task["id"], None, actor_id=ACTOR)
    assert updated["agent_id"] is None


# ── 13. assign nonexistent task ─────────────────────────
def test_assign_nonexistent_task(service, agents):
    agent = _make_agent(agents)
    with pytest.raises(NotFoundError):
        service.assign_agent(MISSING, agent["id"], actor_id=ACTOR)


# ── 14. assign nonexistent agent ────────────────────────
def test_assign_nonexistent_agent(service, agents):
    task = _make_task(service, agents)
    with pytest.raises(NotFoundError):
        service.assign_agent(task["id"], MISSING, actor_id=ACTOR)


# ── 15. list_children ───────────────────────────────────
def test_list_children(service, agents):
    parent = _make_task(service, agents)
    _make_task(service, agents, parent_task_id=parent["id"])
    _make_task(service, agents, parent_task_id=parent["id"])
    _make_task(service, agents)  # unrelated
    children = service.list_children(parent["id"])
    assert len(children) == 2
    assert all(c["parent_task_id"] == parent["id"] for c in children)


# ── 16. list_by_agent ───────────────────────────────────
def test_list_by_agent(service, agents):
    agent = _make_agent(agents)
    _make_task(service, agents, agent_id=agent["id"])
    _make_task(service, agents, agent_id=agent["id"])
    _make_task(service, agents)  # different agent
    rows = service.list_by_agent(agent["id"])
    assert len(rows) == 2
    assert all(r["agent_id"] == agent["id"] for r in rows)


# ── 17. list_by_status ──────────────────────────────────
def test_list_by_status(service, agents):
    _make_task(service, agents, status="running")
    _make_task(service, agents, status="running")
    _make_task(service, agents, status="pending")
    rows = service.list_by_status("running")
    assert len(rows) == 2
    assert all(r["status"] == "running" for r in rows)
    with pytest.raises(ValidationError):
        service.list_by_status("bogus")


# ── 18. list_subtree ────────────────────────────────────
def test_list_subtree(service, agents):
    root = _make_task(service, agents)
    mid = _make_task(service, agents, parent_task_id=root["id"])
    _make_task(service, agents, parent_task_id=mid["id"])
    subtree = service.list_subtree(root["id"])
    assert len(subtree) == 3
    with pytest.raises(ValidationError):
        service.list_subtree("not-a-uuid")


# ── 19. pagination limit ────────────────────────────────
def test_pagination_limit(service, agents):
    for _ in range(5):
        _make_task(service, agents)
    rows = service.list_by_status("pending", limit=2)
    assert len(rows) == 2


# ── 20. pagination offset (deterministic) ───────────────
def test_pagination_offset(service, agents):
    # Unique owner/agent isolation; repository ordering is now
    # (created_at DESC, id DESC) so slices are deterministic.
    agent = _make_agent(agents)
    for _ in range(5):
        _make_task(service, agents, agent_id=agent["id"], status="pending")
    first = service.list_by_agent(agent["id"], limit=2, offset=0)
    second = service.list_by_agent(agent["id"], limit=2, offset=2)
    assert len(second) == 2
    first_ids = {r["id"] for r in first}
    second_ids = {r["id"] for r in second}
    assert first_ids.isdisjoint(second_ids)


# ── 21. invalid pagination ──────────────────────────────
def test_invalid_pagination(service, agents):
    agent = _make_agent(agents)
    with pytest.raises(ValidationError):
        service.list_by_agent(agent["id"], limit=0)
    with pytest.raises(ValidationError):
        service.list_by_agent(agent["id"], limit=101)
    with pytest.raises(ValidationError):
        service.list_by_agent(agent["id"], limit=-1)
    with pytest.raises(ValidationError):
        service.list_by_agent(agent["id"], offset=-1)
    with pytest.raises(ValidationError):
        service.list_by_agent(agent["id"], limit=True)


# ── 22. parent task validation (bad uuid) ───────────────
def test_parent_task_validation(service, agents):
    _make_agent(agents)
    with pytest.raises(ValidationError):
        service.create(title="t", actor_id=ACTOR, parent_task_id="bad")


# ── 23. self-parent rejection (implicit at create) ──────
def test_self_parent_impossible_at_create(service, agents):
    # A task's id is DB-generated, so it can never equal a supplied
    # parent_task_id. We assert the service rejects a non-existent parent
    # rather than allowing a dangling reference.
    with pytest.raises(NotFoundError):
        service.create(
            title="t", actor_id=ACTOR, parent_task_id=MISSING
        )


# ── 24. audit record creation ────────────────────────────
def test_mutation_produces_audit_record(service, agents, db):
    task = _make_task(service, agents)
    audit = AuditService(db)
    rows = audit.list_by_target("task", str(task["id"]))
    assert len(rows) == 1
    assert rows[0]["action"] == "task.create"
    assert rows[0]["outcome"] == "success"
    assert rows[0]["actor_id"] == ACTOR


# ── 25. mutation + audit atomicity (rollback) ───────────
def test_audit_atomicity_rollback(db, agents):
    service = TaskService(db)
    audit = AuditService(db)
    task_id = None
    with pytest.raises(ValueError), transaction(db):
        task = service.create(
            title="boom", actor_id=ACTOR, agent_id=_make_agent(agents)["id"]
        )
        task_id = task["id"]
        raise ValueError("simulated failure")
    with pytest.raises(NotFoundError):
        service.get(task_id)
    assert audit.list_by_target("task", str(task_id)) == []

# ── 26. mutation + audit atomicity (commit) ─────────────
def test_audit_atomicity_commit(db, agents):
    service = TaskService(db)
    audit = AuditService(db)
    task = None
    try:
        with transaction(db):
            task = service.create(
                title="keep", actor_id=ACTOR,
                agent_id=_make_agent(agents)["id"]
            )
        assert service.get(task["id"]) is not None
        rows = audit.list_by_target("task", str(task["id"]))
        assert len(rows) == 1
    finally:
        if task is not None:
            with db.cursor() as cur:
                cur.execute(
                    "DELETE FROM audit_logs WHERE target_id = %s",
                    (str(task["id"]),),
                )
                cur.execute(
                    "DELETE FROM tasks WHERE id = %s", (task["id"],)
                )
            db.commit()


# ── 27. service does not commit ─────────────────────────
def test_service_does_not_commit(service, db, agents):
    _make_task(service, agents)
    assert db.info.transaction_status == TransactionStatus.INTRANS


# ── 28. service does not rollback ───────────────────────
def test_service_does_not_rollback(service, db, agents):
    _make_task(service, agents)
    assert db.info.transaction_status == TransactionStatus.INTRANS


# ── 29. service does not create connections ─────────────
def test_service_does_not_create_connections(monkeypatch, db, agents):
    calls = []

    def _forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("psycopg.connect must not be called by the service")

    import psycopg

    monkeypatch.setattr(psycopg, "connect", _forbidden)
    service = TaskService(db)
    _make_task(service, agents)
    assert calls == [], "TaskService must not open a database connection."


# ── 30. repository delegation ────────────────────────────
def test_repository_is_used(monkeypatch, service, agents):
    called = {"create": False}
    original = TaskRepository.create

    def _spy(self, *args, **kwargs):
        called["create"] = True
        return original(self, *args, **kwargs)

    monkeypatch.setattr(TaskRepository, "create", _spy)
    _make_task(service, agents)
    assert called["create"] is True


# ── 31. no task-lifecycle capability policy enforced ────
def test_no_task_capability_policy_enforced(service, agents):
    # The current capability catalog defines no task capability, so task
    # operations are unconditionally callable at the service layer. This
    # documents the boundary rather than inventing policy.
    task = _make_task(service, agents)
    assert task["id"] is not None
    updated = service.update_status(task["id"], "running", actor_id=ACTOR)
    assert updated["status"] == "running"