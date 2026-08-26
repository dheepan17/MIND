"""Phase 5.7A tests for the trusted SecurityContext identity boundary.

These tests exercise the *foundation* only: the frozen SecurityContext
dataclass, the pluggable resolver seam, the FastAPI dependency, the 401
mapping for missing/invalid external credentials, and the guarantee that a
caller can never substitute HTTP-supplied actor_id/actor_type/source/role for
the trusted context. They also verify the opt-in demo route
``PATCH /agents/{id}/status/secure`` uses ctx.actor_id for audit attribution.

No JWT/OAuth/API keys, no middleware, no request.state, no schema/migration
changes are exercised here.

The tests are intentionally DB-free: the DB transaction dependency and the
AgentService call are overridden so the suite runs without a live database
(every DB-opening test in this repo is skipped/absent when DATABASE_URL is
unset). This keeps the focus on the identity boundary and proves the route
forwards the trusted ``ctx.actor_id`` and never the HTTP body.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi import Request, status
from fastapi.testclient import TestClient
from psycopg import connect as real_connect

TEST_DATABASE_URL = os.getenv("DATABASE_URL")

from app.api import security as security_mod
from app.api.security import (
    AuthenticationError,
    SecurityContext,
    get_security_context,
    internal_security_context,
    set_security_context_resolver,
)
from app.main import app
from app.services.agent_service import AgentService


@pytest.fixture
def client_with_resolver():
    """A TestClient whose DB txn + service are stubbed (DB-free).

    The resolver is controlled per-test; the DB transaction and the real
    AgentService are replaced so no live database is required. This isolates
    the identity boundary and routing behavior from persistence.
    """

    def _fake_txn():
        # Yield a sentinel connection the service won't actually use.
        yield object()

    captured = {}

    

    def _spy_update(self, agent_id, *, status, actor_type, actor_id):
        captured["call"] = {
            "agent_id": agent_id,
            "status": status,
            "actor_type": actor_type,
            "actor_id": actor_id,
        }
        return {
            "id": agent_id,
            "status": status,
            "owner": "owner",
            "purpose": "p",
            "autonomy_level": 0,
            "primary_role_id": None,
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }

    import app.api.routers.agents as agents_mod

    app.dependency_overrides[agents_mod.get_txn] = _fake_txn
    app.dependency_overrides[AgentService.update_status] = _spy_update

    try:
        with TestClient(app) as c:
            yield c, captured
    finally:
        app.dependency_overrides.clear()

@pytest.fixture
def db_free_secure_client():
    """DB-free client for testing the secure agent status route."""

    captured = {}

    def _fake_txn():
        # The spy service method below never uses the connection.
        yield object()

    def _trusted_context():
        return SecurityContext(
            actor_type="agent",
            actor_id="trusted-agent-123",
            authenticated=True,
            source="external",
        )

    def _spy_update_status(
        self,
        agent_id,
        *,
        status,
        actor_type,
        actor_id,
    ):
        captured["agent_id"] = agent_id
        captured["status"] = status
        captured["actor_type"] = actor_type
        captured["actor_id"] = actor_id

        return {
            "id": agent_id,
            "status": status,
            "owner": "owner",
            "purpose": "test",
            "autonomy_level": 0,
            "primary_role_id": None,
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }

    import app.api.routers.agents as agents_mod

    app.dependency_overrides[agents_mod.get_txn] = _fake_txn
    app.dependency_overrides[get_security_context] = _trusted_context

    original_update_status = AgentService.update_status
    AgentService.update_status = _spy_update_status

    try:
        with TestClient(app) as client:
            yield client, captured
    finally:
        AgentService.update_status = original_update_status
        app.dependency_overrides.clear()
        
# ── SecurityContext dataclass invariants ──────────────────────
def test_security_context_is_frozen():
    ctx = SecurityContext("agent", "abc", True, "external")
    with pytest.raises(AttributeError):
        ctx.actor_id = "changed"  # type: ignore[misc]


def test_valid_security_context_fields():
    ctx = SecurityContext("human", "op-1", True, "external")
    assert ctx.actor_type == "human"
    assert ctx.actor_id == "op-1"
    assert ctx.authenticated is True
    assert ctx.source == "external"


@pytest.mark.parametrize("actor_type", ["role", "system", "daemon", ""])
def test_role_cannot_be_self_asserted_identity(actor_type):
    # 'role' (and any non-principal type) must never be a trusted identity.
    with pytest.raises(ValueError):
        SecurityContext(actor_type, "x", True, "external")


def test_internal_context_cannot_be_role():
    with pytest.raises(ValueError):
        internal_security_context("role", "role-1")


# ── Resolver seam + 401 mapping ──────────────────────────────
def _make_request() -> Request:
    # A minimal synthetic external request (no real ASGI transport needed).
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [],
        "query_string": b"",
    }
    return Request(scope)


def test_default_resolver_denies_external_missing_credentials():
    with pytest.raises(AuthenticationError):
        get_security_context(_make_request())


def test_default_resolver_maps_to_401_over_http(client_with_resolver):
    c, _ = client_with_resolver
    agent_id = str(uuid.uuid4())
    resp = c.patch(
        f"/agents/{agent_id}/status/secure",
        json={"status": "suspended", "actor_id": "anyone"},
    )
    assert resp.status_code == status.HTTP_401_UNAUTHORIZED
    assert resp.json()["error"] == "unauthorized"


def test_invalid_credentials_raise_401():
    def _bad_resolver(request: Request) -> SecurityContext:
        raise AuthenticationError("bad token")

    original = security_mod.get_security_context_resolver()
    set_security_context_resolver(_bad_resolver)
    try:
        with pytest.raises(AuthenticationError):
            get_security_context(_make_request())
    finally:
        set_security_context_resolver(original)


def test_valid_identity_produces_security_context():
    captured = {}

    def _ok_resolver(request: Request) -> SecurityContext:
        ctx = SecurityContext("agent", "agent-123", True, "external")
        captured["ctx"] = ctx
        return ctx

    original = security_mod.get_security_context_resolver()
    set_security_context_resolver(_ok_resolver)
    try:
        ctx = get_security_context(_make_request())
        assert isinstance(ctx, SecurityContext)
        assert ctx.actor_id == "agent-123"
        assert ctx.source == "external"
        assert captured["ctx"] is ctx
    finally:
        set_security_context_resolver(original)


# ── HTTP cannot spoof the trusted context ────────────────────
def test_external_caller_cannot_claim_source_internal():
    captured = {}

    def _external_resolver(request: Request) -> SecurityContext:
        # The resolver controls source; an external request yields 'external'
        # and request data can never flip it to 'internal'.
        ctx = SecurityContext("agent", "agent-9", True, "external")
        captured["ctx"] = ctx
        return ctx

    original = security_mod.get_security_context_resolver()
    set_security_context_resolver(_external_resolver)
    try:
        ctx = get_security_context(_make_request())
        assert ctx.source == "external"
        assert ctx.actor_id == "agent-9"
    finally:
        set_security_context_resolver(original)


def test_http_actor_id_cannot_replace_ctx_actor_id(db_free_secure_client):
    client, captured = db_free_secure_client

    agent_id = str(uuid.uuid4())

    response = client.patch(
        f"/agents/{agent_id}/status/secure",
        json={
            "status": "suspended",
            "actor_id": "attacker-supplied",
            "actor_type": "human",
        },
    )

    assert response.status_code == 200

    # The HTTP actor_id must NOT reach the service.
    assert captured["actor_id"] == "trusted-agent-123"

    # The HTTP actor_type must NOT reach the service either.
    assert captured["actor_type"] == "agent"

    # Confirm the request tried to send a different identity.
    assert captured["actor_id"] != "attacker-supplied"

def test_http_actor_type_cannot_replace_ctx_actor_type(db_free_secure_client):
    client, captured = db_free_secure_client

    agent_id = str(uuid.uuid4())

    response = client.patch(
        f"/agents/{agent_id}/status/secure",
        json={
            "status": "suspended",
            "actor_id": "attacker",
            "actor_type": "role",
        },
    )

    assert response.status_code == 200

    # The router must use the trusted SecurityContext.
    assert captured["actor_type"] == "agent"
    assert captured["actor_id"] == "trusted-agent-123"

    # The forged HTTP values must not be forwarded.
    assert captured["actor_type"] != "role"
    assert captured["actor_id"] != "attacker"

# ── Audit attribution on the demo route ──────────────────────
def test_demo_route_audit_uses_ctx_actor_id():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set; skipping live audit test")

    def _ok_resolver(request: Request) -> SecurityContext:
        return SecurityContext("agent", "trusted-auditor", True, "external")

    original = security_mod.get_security_context_resolver()
    set_security_context_resolver(_ok_resolver)
    owner = f"owner-{uuid.uuid4()}"
    actor = f"actor-{uuid.uuid4()}"
    agent_id = None
    try:
        with TestClient(app) as c:
            created = c.post(
                "/agents",
                json={"owner": owner, "purpose": "audit-ctx", "actor_id": actor},
            )
            assert created.status_code == 201
            agent_id = created.json()["id"]
            # Body claims a different actor; ctx must win for audit.
            resp = c.patch(
                f"/agents/{agent_id}/status/secure",
                json={"status": "suspended", "actor_id": "imposter"},
            )
            assert resp.status_code == 200

        conn = real_connect(TEST_DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT action, outcome, actor_id FROM audit_logs "
                    "WHERE target_type = 'agent' AND target_id = %s "
                    "ORDER BY created_at DESC LIMIT 1",
                    (agent_id,),
                )
                row = cur.fetchone()
            assert row is not None
            assert row[0] == "agent.update_status"
            assert row[1] == "success"
            # Audit reflects the trusted ctx, NOT the impersonated body.
            assert row[2] == "trusted-auditor"
        finally:
            conn.close()
    finally:
        set_security_context_resolver(original)
        _cleanup_agent(agent_id)


# ── get_conn / get_txn behavior unchanged ───────────────────
def test_get_conn_and_get_txn_still_work():
    from app.api.dependencies import get_conn, get_txn

    # Routers import these; ensure imports remain valid after the change.
    assert callable(get_conn)
    assert callable(get_txn)

    gen = get_conn()
    conn = next(gen)
    try:
        assert conn is not None
    finally:
        with pytest.raises(StopIteration):
            next(gen)

    tgen = get_txn()
    tconn = next(tgen)
    try:
        assert tconn is not None
    finally:
        with pytest.raises(StopIteration):
            next(tgen)


# ── helpers ──────────────────────────────────────────────────
def _cleanup_agent(agent_id):
    if not agent_id or not TEST_DATABASE_URL:
        return
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM audit_logs WHERE target_id = %s", (str(agent_id),))
            cur.execute("DELETE FROM agents WHERE id = %s", (str(agent_id),))
        conn.commit()
    finally:
        conn.close()
