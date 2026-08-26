"""Phase 4.2 tests for PermissionService.

These run against the isolated ``mind_test_db`` via the shared ``db``
fixture (which rolls back after each test). They exercise real
authorization semantics: roles, capabilities, grants, expiry, and
soft revocation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from psycopg.pq import TransactionStatus

from app.repositories.agents import AgentRepository
from app.repositories.permissions import PermissionRepository
from app.services import (
    AuthorizationError,
    NotFoundError,
    PermissionService,
    ValidationError,
)

MISSING = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def perms(db):
    return PermissionRepository(db)


@pytest.fixture
def agents(db):
    return AgentRepository(db)


@pytest.fixture
def service(db):
    return PermissionService(db)


def _make_role_cap(perms, role_name, cap_key):
    """Create a role, a capability, and assign the cap to the role."""
    role = perms.create_role(role_name)
    cap = perms.create_capability(cap_key)
    perms.assign_capability_to_role(role["id"], cap["id"])
    return role, cap


def _make_agent(agents, role_id=None):
    return agents.create(owner="owner", purpose="p", primary_role_id=role_id)


# ── 1. role-derived capability ───────────────────────────
def test_agent_with_role_receives_role_capability(service, perms, agents):
    role, _cap = _make_role_cap(perms, "reader", "read")
    agent = _make_agent(agents, role_id=role["id"])
    assert service.get_effective_capabilities(agent["id"]) == {"read"}


# ── 2. direct grant capability ────────────────────────────
def test_agent_with_direct_grant_receives_capability(service, perms, agents):
    agent = _make_agent(agents)
    cap = perms.create_capability("write")
    perms.grant(agent_id=agent["id"], capability_id=cap["id"])
    assert service.get_effective_capabilities(agent["id"]) == {"write"}


# ── 3. role + direct grant UNION ─────────────────────────
def test_role_and_grant_union(service, perms, agents):
    role, _role_cap = _make_role_cap(perms, "reader", "read")
    agent = _make_agent(agents, role_id=role["id"])
    direct_cap = perms.create_capability("write")
    perms.grant(agent_id=agent["id"], capability_id=direct_cap["id"])
    assert service.get_effective_capabilities(agent["id"]) == {"read", "write"}


# ── 4. duplicate capability appears once ─────────────────
def test_duplicate_capability_appears_once(service, perms, agents):
    role, cap = _make_role_cap(perms, "reader", "read")
    agent = _make_agent(agents, role_id=role["id"])
    # Same capability granted directly as well.
    perms.grant(agent_id=agent["id"], capability_id=cap["id"])
    eff = service.get_effective_capabilities(agent["id"])
    assert eff == {"read"}
    assert len(eff) == 1


# ── 5. revoked direct grant not effective ────────────────
def test_revoked_direct_grant_not_effective(service, perms, agents):
    agent = _make_agent(agents)
    cap = perms.create_capability("write")
    grant = perms.grant(agent_id=agent["id"], capability_id=cap["id"])
    perms.revoke(grant["id"])
    assert "write" not in service.get_effective_capabilities(agent["id"])


# ── 6. expired direct grant not effective ────────────────
def test_expired_direct_grant_not_effective(service, perms, agents):
    agent = _make_agent(agents)
    cap = perms.create_capability("write")
    past = datetime.now(timezone.utc) - timedelta(days=1)
    perms.grant(agent_id=agent["id"], capability_id=cap["id"], expires_at=past)
    assert "write" not in service.get_effective_capabilities(agent["id"])


# ── 7. non-expiring direct grant effective ───────────────
def test_non_expiring_direct_grant_effective(service, perms, agents):
    agent = _make_agent(agents)
    cap = perms.create_capability("write")
    perms.grant(agent_id=agent["id"], capability_id=cap["id"], expires_at=None)
    assert "write" in service.get_effective_capabilities(agent["id"])


# ── 8. has_capability True when authorized ───────────────
def test_has_capability_true(service, perms, agents):
    role, _cap = _make_role_cap(perms, "reader", "read")
    agent = _make_agent(agents, role_id=role["id"])
    assert service.has_capability(agent["id"], "read") is True


# ── 9. has_capability False when absent ──────────────────
def test_has_capability_false_when_absent(service, perms, agents):
    agent = _make_agent(agents)
    assert service.has_capability(agent["id"], "read") is False


# ── 10. nonexistent agent raises NotFoundError ───────────
def test_nonexistent_agent_raises_not_found(service):
    with pytest.raises(NotFoundError):
        service.get_effective_capabilities(MISSING)
    with pytest.raises(NotFoundError):
        service.has_capability(MISSING, "read")


# ── 11. require_capability succeeds when authorized ──────
def test_require_capability_succeeds(service, perms, agents):
    role, _cap = _make_role_cap(perms, "reader", "read")
    agent = _make_agent(agents, role_id=role["id"])
    # Returns normally (None) when authorized.
    assert service.require_capability(agent["id"], "read") is None


# ── 12. require_capability raises AuthorizationError ─────
def test_require_capability_raises_authorization_error(service, perms, agents):
    agent = _make_agent(agents)
    with pytest.raises(AuthorizationError):
        service.require_capability(agent["id"], "read")


# ── 13. grant to nonexistent agent raises NotFoundError ──
def test_grant_to_nonexistent_agent_raises_not_found(service, perms):
    cap = perms.create_capability("write")
    with pytest.raises(NotFoundError):
        service.grant_capability(agent_id=MISSING, capability_id=cap["id"])


# ── 14. grant of nonexistent capability raises NotFound ──
def test_grant_nonexistent_capability_raises_not_found(service, agents):
    agent = _make_agent(agents)
    with pytest.raises(NotFoundError):
        service.grant_capability(agent_id=agent["id"], capability_id=MISSING)


# ── 15. revoke_capability performs soft revocation ───────
def test_revoke_capability_soft_revokes(service, perms, agents):
    agent = _make_agent(agents)
    cap = perms.create_capability("write")
    grant = perms.grant(agent_id=agent["id"], capability_id=cap["id"])
    service.revoke_capability(grant["id"])
    # The grant row is retained but marked revoked.
    grants = perms.list_grants_for_agent(agent["id"])
    assert len(grants) == 1
    assert grants[0]["revoked_at"] is not None


# ── 16. revoked grants remain stored, not effective ──────
def test_revoked_grant_stored_but_not_effective(service, perms, agents):
    agent = _make_agent(agents)
    cap = perms.create_capability("write")
    grant = perms.grant(agent_id=agent["id"], capability_id=cap["id"])
    service.revoke_capability(grant["id"])
    # Still present in the store...
    assert len(perms.list_grants_for_agent(agent["id"])) == 1
    # ...but no longer effective.
    assert "write" not in service.get_effective_capabilities(agent["id"])


# ── 17. transaction ownership stays outside the service ──
def test_service_does_not_commit_transaction(service, perms, agents, db):
    agent = _make_agent(agents)
    cap = perms.create_capability("write")
    # A service write must NOT commit; the connection must remain in a
    # transaction (the caller owns commit/rollback).
    service.grant_capability(agent_id=agent["id"], capability_id=cap["id"])
    assert db.info.transaction_status == TransactionStatus.INTRANS


# ── input validation ─────────────────────────────────────
def test_grant_capability_validates_inputs(service):
    with pytest.raises(ValidationError):
        service.grant_capability(agent_id="", capability_id="x")
    with pytest.raises(ValidationError):
        service.grant_capability(agent_id="x", capability_id="")
    with pytest.raises(ValidationError):
        service.revoke_capability("")


def test_has_capability_validates_inputs(service):
    with pytest.raises(ValidationError):
        service.has_capability("", "read")
    with pytest.raises(ValidationError):
        service.has_capability("x", "")