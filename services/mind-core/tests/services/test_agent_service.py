"""Phase 4.4 tests for AgentService.

These run against the isolated ``mind_test_db`` via the shared ``db``
fixture (which rolls back after each test). They exercise the agent
business boundary: creation, reads, status/autonomy mutations, listing,
pagination, validation, NotFoundError semantics, audit integration, and
transaction ownership (the service never commits or rolls back itself).
"""
from __future__ import annotations

import uuid

import pytest
from psycopg.pq import TransactionStatus

from app.db.transaction import transaction
from app.repositories.agents import AgentRepository
from app.repositories.permissions import PermissionRepository
from app.services import (
    AgentService,
    AuditService,
    NotFoundError,
    ValidationError,
)

MISSING = "00000000-0000-0000-0000-000000000000"
OWNER = "owner-11111111-1111-1111-1111-111111111111"
ACTOR = "actor-22222222-2222-2222-2222-222222222222"


@pytest.fixture
def service(db):
    return AgentService(db)


@pytest.fixture
def perms(db):
    return PermissionRepository(db)


def _make_agent(service, **overrides):
    """Create an agent through the service with sensible defaults."""
    payload = {
        "owner": OWNER,
        "purpose": "test purpose",
        "actor_id": ACTOR,
    }
    payload.update(overrides)
    return service.create(**payload)


# ── 1. create agent ──────────────────────────────────────
def test_create_agent(service):
    agent = _make_agent(service)
    assert agent["id"] is not None
    assert agent["owner"] == OWNER
    assert agent["purpose"] == "test purpose"
    assert agent["status"] == "active"
    assert agent["autonomy_level"] == 0


# ── 2. get existing agent ────────────────────────────────
def test_get_existing_agent(service):
    agent = _make_agent(service)
    fetched = service.get(agent["id"])
    assert fetched["id"] == agent["id"]
    assert fetched["owner"] == OWNER


# ── 3. get nonexistent agent ─────────────────────────────
def test_get_nonexistent_agent(service):
    with pytest.raises(NotFoundError):
        service.get(MISSING)


# ── 4. update status ─────────────────────────────────────
def test_update_status(service):
    agent = _make_agent(service)
    updated = service.update_status(agent["id"], "suspended", actor_id=ACTOR)
    assert updated["status"] == "suspended"
    assert service.get(agent["id"])["status"] == "suspended"


# ── 5. update autonomy level ─────────────────────────────
def test_update_autonomy_level(service):
    agent = _make_agent(service)
    updated = service.update_autonomy_level(
        agent["id"], 2, actor_id=ACTOR
    )
    assert updated["autonomy_level"] == 2
    assert service.get(agent["id"])["autonomy_level"] == 2


# ── 6. list by owner ─────────────────────────────────────
def test_list_by_owner(service):
    # Unique owner isolates this test from any committed rows left in the
    # persistent test DB by other runs (no global wipe / truncate).
    owner = f"owner-{uuid.uuid4()}"
    _make_agent(service, owner=owner)
    _make_agent(service, owner=owner)
    _make_agent(service, owner="other-owner")
    rows = service.list_by_owner(owner)
    assert len(rows) == 2
    assert all(r["owner"] == owner for r in rows)


# ── 7. list by role ──────────────────────────────────────
def test_list_by_role(service, perms):
    role = perms.create_role("operator")
    _make_agent(service, primary_role_id=role["id"])
    _make_agent(service, primary_role_id=role["id"])
    _make_agent(service)  # no role
    rows = service.list_by_role(role["id"])
    assert len(rows) == 2
    assert all(r["primary_role_id"] == role["id"] for r in rows)


# ── 8. pagination limit ──────────────────────────────────
def test_pagination_limit(service):
    for _ in range(5):
        _make_agent(service)
    rows = service.list_by_owner(OWNER, limit=2)
    assert len(rows) == 2


# ── 9. pagination offset ─────────────────────────────────
def test_pagination_offset(service):
    # Unique owner isolates from committed leftover rows; the repository's
    # (created_at DESC, id DESC) ordering makes the slices deterministic.
    owner = f"owner-{uuid.uuid4()}"
    for _ in range(5):
        _make_agent(service, owner=owner)
    first = service.list_by_owner(owner, limit=2, offset=0)
    second = service.list_by_owner(owner, limit=2, offset=2)
    assert len(second) == 2
    first_ids = {r["id"] for r in first}
    second_ids = {r["id"] for r in second}
    assert first_ids.isdisjoint(second_ids)


# ── 10. invalid limit ────────────────────────────────────
def test_invalid_limit(service):
    with pytest.raises(ValidationError):
        service.list_by_owner(OWNER, limit=0)
    with pytest.raises(ValidationError):
        service.list_by_owner(OWNER, limit=101)
    with pytest.raises(ValidationError):
        service.list_by_owner(OWNER, limit=-1)


# ── 11. invalid offset ───────────────────────────────────
def test_invalid_offset(service):
    with pytest.raises(ValidationError):
        service.list_by_owner(OWNER, offset=-1)


# ── 12. invalid required input ───────────────────────────
def test_invalid_required_input(service):
    with pytest.raises(ValidationError):
        service.create(owner="", purpose="p", actor_id=ACTOR)
    with pytest.raises(ValidationError):
        service.create(owner="o", purpose="", actor_id=ACTOR)
    with pytest.raises(ValidationError):
        service.list_by_owner("")
    with pytest.raises(ValidationError):
        service.list_by_role("not-a-uuid")
    with pytest.raises(ValidationError):
        service.get("not-a-uuid")


# ── 13. invalid status ───────────────────────────────────
def test_invalid_status(service):
    agent = _make_agent(service)
    with pytest.raises(ValidationError):
        service.update_status(agent["id"], "bogus", actor_id=ACTOR)


# ── 14. invalid autonomy level ──────────────────────────
def test_invalid_autonomy_level(service):
    agent = _make_agent(service)
    with pytest.raises(ValidationError):
        service.update_autonomy_level(agent["id"], 5, actor_id=ACTOR)
    with pytest.raises(ValidationError):
        service.update_autonomy_level(agent["id"], -1, actor_id=ACTOR)
    with pytest.raises(ValidationError):
        service.update_autonomy_level(agent["id"], "high", actor_id=ACTOR)
    with pytest.raises(ValidationError):
        service.create(
            owner=OWNER, purpose="p", autonomy_level=3, actor_id=ACTOR
        )


# ── 15. mutation produces audit record ──────────────────
def test_mutation_produces_audit_record(service, db):
    agent = _make_agent(service)
    audit = AuditService(db)
    # audit_logs.target_id is text; the agent id is stored as its string form.
    rows = audit.list_by_target("agent", str(agent["id"]))
    assert len(rows) == 1
    assert rows[0]["action"] == "agent.create"
    assert rows[0]["outcome"] == "success"
    assert rows[0]["actor_id"] == ACTOR


# ── 16. audit is in same transaction ─────────────────────
def test_audit_in_same_transaction(db):
    service = AgentService(db)
    audit = AuditService(db)
    with transaction(db):
        agent = service.create(owner=OWNER, purpose="p", actor_id=ACTOR)
        # Uncommitted, but visible within the same caller transaction.
        rows = audit.list_by_target("agent", str(agent["id"]))
        assert len(rows) == 1


# ── 17. service does not commit ─────────────────────────
def test_service_does_not_commit(service, db):
    _make_agent(service)
    # The service must NOT commit; the connection stays in-transaction
    # so the caller owns commit/rollback.
    assert db.info.transaction_status == TransactionStatus.INTRANS


# ── 18. service does not rollback ───────────────────────
def test_service_does_not_rollback(service, db):
    _make_agent(service)
    # A rollback would move the connection to IDLE; it must remain INTRANS.
    assert db.info.transaction_status == TransactionStatus.INTRANS


# ── 19. service does not create connections ─────────────
def test_service_does_not_create_connections(monkeypatch, db):
    # If the service (or its collaborators) opened a connection, this would
    # raise. Constructing and using the service must not call connect().
    calls = []

    def _forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("psycopg.connect must not be called by the service")

    import psycopg

    monkeypatch.setattr(psycopg, "connect", _forbidden)
    service = AgentService(db)
    _make_agent(service)
    assert calls == [], "AgentService must not open a database connection."


# ── 20. repository is used rather than SQL ───────────────
def test_repository_is_used(monkeypatch, service):
    called = {"create": False}
    original = AgentRepository.create

    def _spy(self, *args, **kwargs):
        called["create"] = True
        return original(self, *args, **kwargs)

    monkeypatch.setattr(AgentRepository, "create", _spy)
    _make_agent(service)
    assert called["create"] is True


# ── 21. no agent-lifecycle capability policy is enforced ─
def test_no_agent_capability_policy_enforced(service):
    # The current capability catalog defines no agent-management
    # capability, so agent operations are unconditionally callable at the
    # service layer. This documents the boundary rather than inventing
    # policy: a plain create must NOT raise AuthorizationError.
    agent = _make_agent(service)
    assert agent["id"] is not None
    # And a mutation on an existing agent is likewise not gated.
    updated = service.update_status(agent["id"], "suspended", actor_id=ACTOR)
    assert updated["status"] == "suspended"


# ── audit atomicity: rollback removes both ──────────────
def test_audit_atomicity_rollback(db):
    service = AgentService(db)
    audit = AuditService(db)
    agent_id = None
    with pytest.raises(ValueError), transaction(db):
        agent = service.create(owner=OWNER, purpose="p", actor_id=ACTOR)
        agent_id = agent["id"]
        # Force the caller's transaction to roll back.
        raise ValueError("simulated failure")
    # Neither the agent nor its audit record may persist. A rolled-back
    # agent is simply absent (get raises NotFoundError), and no audit row
    # references it.
    with pytest.raises(NotFoundError):
        service.get(agent_id)
    assert audit.list_by_target("agent", str(agent_id)) == []


# ── audit atomicity: commit persists both ───────────────
def test_audit_atomicity_commit(db):
    service = AgentService(db)
    audit = AuditService(db)
    agent = None
    try:
        with transaction(db):
            agent = service.create(owner=OWNER, purpose="p", actor_id=ACTOR)
        # After the caller commits, both the agent and its audit record
        # must be visible/persisted.
        assert service.get(agent["id"]) is not None
        rows = audit.list_by_target("agent", str(agent["id"]))
        assert len(rows) == 1
    finally:
        # Test hygiene: remove the committed rows so the suite leaves no
        # persistent data. (Cleanup only; the service never does this.)
        if agent is not None:
            with db.cursor() as cur:
                cur.execute(
                    "DELETE FROM audit_logs WHERE target_id = %s",
                    (str(agent["id"]),),
                )
                cur.execute(
                    "DELETE FROM agents WHERE id = %s", (agent["id"],)
                )
            db.commit()
