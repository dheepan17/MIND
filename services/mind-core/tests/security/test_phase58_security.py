"""Phase 5.8 security tests: trusted-actor authorization at the API boundary.

These tests require ``TEST_DATABASE_URL`` (they hit the isolated test DB) and
skip cleanly when it is unset.

They verify, end-to-end through the real FastAPI app:

* 401 when no resolver is installed (the default 5.7A resolver denies all
  external requests).
* 403 when the authenticated principal lacks the generic ``write`` capability.
* 200 when the authenticated principal holds ``write``.
* A denied mutation is recorded in ``audit_logs`` with ``outcome='denied'``
  and ``capability_used='write'`` (security-relevant denial audit).
* Identity forgery protection: an ``actor_id`` supplied in the request body is
  ignored for audit attribution; the trusted SecurityContext identity is used.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import connect as real_connect
from app.main import app
from app.services.agent_service import AgentService
from app.api.security import set_security_context_resolver, get_security_context_resolver
from tests.security.helpers import (
    delete_agent,
    install_write_resolver,
    _seeded_writer_conn,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def client(monkeypatch):
    """TestClient whose DB dependency targets the isolated test database."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set; skipping live security API tests")
    monkeypatch.setattr(
        "app.api.dependencies.connect",
        lambda url=None: real_connect(TEST_DATABASE_URL),
    )
    with TestClient(app) as c:
        yield c


def _make_anon_agent() -> str:
    """Create (and commit) an agent that holds NO capabilities."""
    conn = real_connect(TEST_DATABASE_URL)
    try:
        from app.repositories.agents import AgentRepository

        agent = AgentRepository(conn).create(
            owner=f"owner-{uuid.uuid4()}", purpose="security-test"
        )
        conn.commit()
        return str(agent["id"])
    finally:
        conn.close()


@pytest.fixture
def writer_agent():
    """A committed agent with the ``write`` capability; cleaned up after."""
    agent_id = _seeded_writer_conn()
    try:
        yield agent_id
    finally:
        delete_agent(agent_id)


@pytest.fixture
def anon_agent():
    """A committed agent without any capability; cleaned up after."""
    agent_id = _make_anon_agent()
    try:
        yield agent_id
    finally:
        delete_agent(agent_id)


@pytest.fixture(autouse=True)
def _reset_resolver():
    """Ensure each test starts from the default (deny-all) resolver."""
    original = get_security_context_resolver()
    try:
        yield
    finally:
        set_security_context_resolver(original)


# ── 1. unauthenticated request -> 401 ──────────────────────────────────
def test_unauthenticated_mutation_returns_401(client):
    resp = client.post(
        "/agents", json={"owner": f"owner-{uuid.uuid4()}", "purpose": "x"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


# ── 2. authenticated but lacking write -> 403 ──────────────────────────
def test_authenticated_without_write_returns_403(client, anon_agent):
    install_write_resolver(anon_agent)
    resp = client.post(
        "/agents", json={"owner": f"owner-{uuid.uuid4()}", "purpose": "x"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "forbidden"


# ── 3. authenticated with write -> 201 ─────────────────────────────────
def test_authenticated_with_write_succeeds(client, writer_agent):
    install_write_resolver(writer_agent)
    resp = client.post(
        "/agents", json={"owner": f"owner-{uuid.uuid4()}", "purpose": "x"}
    )
    assert resp.status_code == 201
    body = resp.json()
    delete_agent(body["id"])
    assert body["owner"]


# ── 4. denied mutation is recorded in audit_logs ───────────────────────
def test_denied_mutation_is_audited(client, anon_agent):
    install_write_resolver(anon_agent)
    resp = client.post(
        "/agents", json={"owner": f"owner-{uuid.uuid4()}", "purpose": "x"}
    )
    assert resp.status_code == 403

    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action, outcome, capability_used, actor_id, target_id "
                "FROM audit_logs WHERE action = 'agent.create' "
                "ORDER BY created_at DESC LIMIT 1"
            )
            row = cur.fetchone()
    finally:
        conn.close()
    assert row is not None, "expected a denied audit record"
    action, outcome, capability_used, actor_id, target_id = row
    assert outcome == "denied"
    assert capability_used == "write"
    assert actor_id == anon_agent
    # No agent was created, so the target_id stays NULL.
    assert target_id is None
    assert action == "agent.create"


# ── 5. identity forgery: body actor_id ignored for attribution ─────────
def test_body_actor_id_is_ignored_for_attribution(client, writer_agent):
    install_write_resolver(writer_agent)
    resp = client.post(
        "/agents", json={"owner": f"owner-{uuid.uuid4()}", "purpose": "x"}
    )
    assert resp.status_code == 201
    created_id = resp.json()["id"]

    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT actor_id FROM audit_logs "
                "WHERE action = 'agent.create' AND target_id = %s",
                (str(created_id),),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    assert rows, "expected a success audit record"
    for (actor_id,) in rows:
        assert actor_id == writer_agent
    delete_agent(created_id)


# ── 6. service-layer denial audit persists even if caller txn rolls back ─
def test_service_denial_audit_persists(client, anon_agent):
    """A denied authorization raises and commits its own audit row on a
    separate connection so it survives the caller rollback."""
    install_write_resolver(anon_agent)
    service = AgentService(real_connect(TEST_DATABASE_URL))
    try:
        with pytest.raises(Exception):
            service.create(
                owner=f"owner-{uuid.uuid4()}", purpose="x", actor_id=anon_agent
            )
        with service._conn.cursor() as cur:
            cur.execute(
                "SELECT outcome, capability_used, actor_id FROM audit_logs "
                "WHERE action = 'agent.create' ORDER BY created_at DESC LIMIT 1"
            )
            row = cur.fetchone()
        assert row is not None
        outcome, capability_used, actor_id = row
        assert outcome == "denied"
        assert capability_used == "write"
        assert actor_id == anon_agent
    finally:
        service._conn.close()

