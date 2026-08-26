"""Phase 4.8 tests for MemoryService.

Run against the isolated ``mind_test_db`` via the shared ``db`` fixture
(which rolls back after each test). They exercise the memory-store
business boundary: creation, reads, updates, soft-deletion, listing by
agent / classification, deterministic pagination, validation,
NotFoundError semantics, transaction ownership (the service never commits
or rolls back itself), the deliberate absence of a hard-delete API, and
the audit trail for security-sensitive memory mutations.

All identifiers are unique per test so the suite is self-isolating and
never relies on a clean persistent DB or global truncation.
"""
from __future__ import annotations

import uuid

import pytest
from psycopg.pq import TransactionStatus

from app.db.transaction import transaction
from app.repositories.agents import AgentRepository
from app.repositories.memories import MemoryRepository
from app.services import (
    AuditService,
    MemoryService,
    NotFoundError,
    ValidationError,
)

MISSING = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def service(db):
    return MemoryService(db)


@pytest.fixture
def agents(db):
    return AgentRepository(db)


def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def _make_agent(agents) -> dict:
    return agents.create(owner=_uid("owner"), purpose="p")


def _make_memory(service, agents, **overrides) -> dict:
    # MemoryService.create() takes keyword-only arguments. The helper keeps
    # them as distinct arguments (it must NOT merge them into one dict) and
    # only auto-assigns an agent when the caller did not supply one.
    kwargs: dict = {
        "agent_id": _make_agent(agents)["id"],
        "type": "semantic",
        "content": "remember this",
    }
    kwargs.update(overrides)
    return service.create(**kwargs)


# 1. create basic memory
def test_create_basic_memory(service, agents):
    agent = _make_agent(agents)
    mem = service.create(
        agent_id=agent["id"], type="semantic", content="hidden fact"
    )
    assert mem["id"] is not None
    assert mem["agent_id"] == agent["id"]
    assert mem["type"] == "semantic"
    assert mem["content"] == "hidden fact"
    assert mem["classification"] == "internal"  # default
    assert mem["embedding"] is None
    assert mem["metadata"] is None
    assert mem["deleted_at"] is None


# 2. create with all optional fields
def test_create_with_optional_fields(service, agents):
    agent = _make_agent(agents)
    mem = service.create(
        agent_id=agent["id"],
        type="episodic",
        content="c",
        classification="secret",
        embedding=[0.1, 0.2, 0.3],
        metadata={"src": "chat"},
    )
    assert mem["classification"] == "secret"
    assert mem["embedding"] == [0.1, 0.2, 0.3]
    assert mem["metadata"] == {"src": "chat"}


# 3. agent existence validation
def test_create_nonexistent_agent_rejected(service):
    with pytest.raises(NotFoundError):
        service.create(agent_id=MISSING, type="semantic", content="x")


# 4. get existing memory
def test_get_existing_memory(service, agents):
    mem = _make_memory(service, agents)
    fetched = service.get(mem["id"])
    assert fetched["id"] == mem["id"]
    assert fetched["content"] == "remember this"


# 5. get nonexistent / invalid id
def test_get_nonexistent_memory(service):
    with pytest.raises(NotFoundError):
        service.get(MISSING)
    with pytest.raises(ValidationError):
        service.get("not-a-uuid")


# 6. update content
def test_update_content(service, agents):
    mem = _make_memory(service, agents)
    updated = service.update(mem["id"], content="revised", actor_id="agent-1")
    assert updated["content"] == "revised"
    assert service.get(mem["id"])["content"] == "revised"


# 7. update classification
def test_update_classification(service, agents):
    mem = _make_memory(service, agents)
    updated = service.update(
        mem["id"], classification="secret", actor_id="agent-1"
    )
    assert updated["classification"] == "secret"


# 8. update embedding
def test_update_embedding(service, agents):
    mem = _make_memory(service, agents)
    updated = service.update(
        mem["id"], embedding=[1.0, 2.0], actor_id="agent-1"
    )
    assert updated["embedding"] == [1.0, 2.0]


# 9. update metadata
def test_update_metadata(service, agents):
    mem = _make_memory(service, agents)
    updated = service.update(
        mem["id"], metadata={"k": "v"}, actor_id="agent-1"
    )
    assert updated["metadata"] == {"k": "v"}


# 10. update no-op (no fields) returns unchanged, no audit
def test_update_noop_returns_unchanged_and_no_audit(service, agents, db):
    mem = _make_memory(service, agents)
    updated = service.update(mem["id"], actor_id="agent-1")
    assert updated["id"] == mem["id"]
    audit = AuditService(db)
    # A no-op update must not produce a *second* (memory.update) audit
    # record; only the original memory.create from _make_memory remains.
    rows = audit.list_by_target("memory", str(mem["id"]))
    assert len(rows) == 1
    assert rows[0]["action"] == "memory.create"


# 11. update nonexistent memory
def test_update_nonexistent_memory(service):
    with pytest.raises(NotFoundError):
        service.update(MISSING, content="x", actor_id="agent-1")


# 12. soft_delete basic
def test_soft_delete(service, agents):
    mem = _make_memory(service, agents)
    deleted = service.soft_delete(mem["id"], actor_id="agent-1")
    assert deleted["deleted_at"] is not None
    with pytest.raises(NotFoundError):
        service.get(mem["id"])


# 13. soft_delete nonexistent memory
def test_soft_delete_nonexistent_memory(service):
    with pytest.raises(NotFoundError):
        service.soft_delete(MISSING, actor_id="agent-1")


# 14. list_by_agent
def test_list_by_agent(service, agents):
    agent = _make_agent(agents)
    other = _make_agent(agents)
    service.create(agent_id=agent["id"], type="working", content="c1")
    service.create(agent_id=agent["id"], type="working", content="c2")
    service.create(agent_id=other["id"], type="working", content="c3")
    rows = service.list_by_agent(agent["id"])
    assert len(rows) == 2
    assert all(r["agent_id"] == agent["id"] for r in rows)


# 15. list_by_classification
def test_list_by_classification(service, agents):
    agent = _make_agent(agents)
    service.create(
        agent_id=agent["id"], type="semantic", content="a",
        classification="secret",
    )
    service.create(
        agent_id=agent["id"], type="semantic", content="b",
        classification="secret",
    )
    service.create(
        agent_id=agent["id"], type="semantic", content="c",
        classification="internal",
    )
    rows = service.list_by_classification("secret")
    assert len(rows) == 2
    assert all(r["classification"] == "secret" for r in rows)


# 16. pagination limit
def test_pagination_limit(service, agents):
    agent = _make_agent(agents)
    for i in range(5):
        service.create(agent_id=agent["id"], type="working", content=f"c{i}")
    rows = service.list_by_agent(agent["id"], limit=2)
    assert len(rows) == 2


# 17. pagination offset (deterministic)
def test_pagination_offset_deterministic(service, agents):
    agent = _make_agent(agents)
    for i in range(5):
        service.create(agent_id=agent["id"], type="working", content=f"c{i}")
    first = service.list_by_agent(agent["id"], limit=2, offset=0)
    second = service.list_by_agent(agent["id"], limit=2, offset=2)
    assert len(second) == 2
    first_ids = {r["id"] for r in first}
    second_ids = {r["id"] for r in second}
    assert first_ids.isdisjoint(second_ids)

# 18. invalid pagination
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
    with pytest.raises(ValidationError):
        service.list_by_agent(agent["id"], offset=True)


# 19. validation: invalid required strings
def test_create_validation_strings(service, agents):
    agent = _make_agent(agents)
    with pytest.raises(ValidationError):
        service.create(agent_id=agent["id"], type="", content="x")
    with pytest.raises(ValidationError):
        service.create(agent_id=agent["id"], type=123, content="x")
    with pytest.raises(ValidationError):
        service.create(agent_id=agent["id"], type="semantic", content="")
    with pytest.raises(ValidationError):
        service.create(agent_id=agent["id"], type="semantic", content=None)


# 20. validation: invalid classification / embedding / metadata
def test_create_validation_domain(service, agents):
    agent = _make_agent(agents)
    with pytest.raises(ValidationError):
        service.create(
            agent_id=agent["id"], type="semantic", content="x",
            classification="",
        )
    with pytest.raises(ValidationError):
        service.create(
            agent_id=agent["id"], type="semantic", content="x",
            embedding="not-a-list",
        )
    with pytest.raises(ValidationError):
        service.create(
            agent_id=agent["id"], type="semantic", content="x",
            metadata=[1, 2, 3],
        )


# 21. validation: invalid UUIDs
def test_create_validation_uuids(service):
    with pytest.raises(ValidationError):
        service.create(agent_id="not-a-uuid", type="semantic", content="x")


# 22. immutability: no hard-delete API on service
def test_service_has_no_hard_delete_api(service):
    for name in ("delete", "remove", "purge", "hard_delete"):
        assert not hasattr(service, name), (
            f"MemoryService must not expose {name}()."
        )


# 23. immutability: repository remains soft-delete only
def test_repository_is_soft_delete_only(db):
    repo = MemoryRepository(db)
    for name in ("delete", "remove", "purge", "hard_delete"):
        assert not hasattr(repo, name), (
            f"MemoryRepository must not expose {name}()."
        )


# 24. repository delegation
def test_repository_is_used(monkeypatch, service, agents):
    called = {"create": False}
    original = MemoryRepository.create

    def _spy(self, *args, **kwargs):
        called["create"] = True
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MemoryRepository, "create", _spy)
    _make_memory(service, agents)
    assert called["create"] is True


# 25. audit trail for create
def test_audit_on_create(service, agents, db):
    mem = _make_memory(service, agents, classification="secret")
    audit = AuditService(db)
    rows = audit.list_by_target("memory", str(mem["id"]))
    assert len(rows) == 1
    assert rows[0]["action"] == "memory.create"
    assert rows[0]["outcome"] == "success"
    assert rows[0]["context"]["classification"] == "secret"


# 26. audit trail for update
def test_audit_on_update(service, agents, db):
    mem = _make_memory(service, agents)
    service.update(mem["id"], content="new", actor_id="agent-1")
    audit = AuditService(db)
    rows = [
        r for r in audit.list_by_target("memory", str(mem["id"]))
        if r["action"] == "memory.update"
    ]
    assert len(rows) == 1
    assert "content" in rows[0]["context"]["fields"]


# 27. audit trail for soft_delete
def test_audit_on_soft_delete(service, agents, db):
    mem = _make_memory(service, agents)
    service.soft_delete(mem["id"], actor_id="agent-1")
    audit = AuditService(db)
    rows = [
        r for r in audit.list_by_target("memory", str(mem["id"]))
        if r["action"] == "memory.soft_delete"
    ]
    assert len(rows) == 1


# 28. no audit for reads
def test_no_audit_for_reads(service, agents, db):
    mem = _make_memory(service, agents)
    service.get(mem["id"])
    service.list_by_agent(mem["agent_id"])
    audit = AuditService(db)
    assert len(audit.list_by_target("memory", str(mem["id"]))) == 1


# 29. mutation + atomicity (rollback)
def test_atomicity_rollback(db, agents):
    service = MemoryService(db)
    memory_id = None
    with pytest.raises(ValueError), transaction(db):
        memory_id = service.create(
            agent_id=_make_agent(agents)["id"],
            type="semantic",
            content="x",
            actor_id="agent-1",
        )["id"]
        raise ValueError("simulated failure")
    with pytest.raises(NotFoundError):
        service.get(memory_id)


# 30. mutation + atomicity (commit)
def test_atomicity_commit(db, agents):
    service = MemoryService(db)
    mem = None
    try:
        with transaction(db):
            mem = service.create(
                agent_id=_make_agent(agents)["id"],
                type="semantic",
                content="x",
                actor_id="agent-1",
            )
        assert service.get(mem["id"]) is not None
    finally:
        if mem is not None:
            with db.cursor() as cur:
                cur.execute(
                    "DELETE FROM memories WHERE id = %s", (mem["id"],)
                )
            db.commit()


# 31. service does not commit
def test_service_does_not_commit(service, db, agents):
    _make_memory(service, agents)
    assert db.info.transaction_status == TransactionStatus.INTRANS


# 32. service does not rollback
def test_service_does_not_rollback(service, db, agents):
    _make_memory(service, agents)
    assert db.info.transaction_status == TransactionStatus.INTRANS


# 33. service does not create connections
def test_service_does_not_create_connections(monkeypatch, db, agents):
    calls = []

    def _forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("psycopg.connect must not be called by the service")

    import psycopg

    monkeypatch.setattr(psycopg, "connect", _forbidden)
    service = MemoryService(db)
    _make_memory(service, agents)
    assert calls == [], "MemoryService must not open a database connection."


# 34. caller-owned transaction remains active
def test_caller_transaction_stays_active(service, db, agents):
    with transaction(db):
        _make_memory(service, agents)
        assert db.info.transaction_status == TransactionStatus.INTRANS