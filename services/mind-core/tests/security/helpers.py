"""Shared helpers for Phase 5.8 security tests (trusted-actor authorization).

These helpers spin up an isolated test database transaction, seed a real
agent with the generic ``write`` capability (via a role), and return a
:class:`SecurityContext` that resolves to that agent. They let API/security
tests authenticate real, capability-bearing principals against the live
``mind_test_db`` exactly the way a future auth mechanism would, without
inventing any concrete credential exchange (Phase 5.7A scope).
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.api.dependencies import connect as real_connect
from app.api.security import (
    SecurityContext,
    set_security_context_resolver,
)
from app.db.transaction import transaction
from app.repositories.agents import AgentRepository
from app.repositories.permissions import PermissionRepository

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def _seeded_writer_conn() -> str:
    """Create (and commit) a single agent that holds the generic ``write`` cap.

    Returns the agent id. The row is committed so it survives across
    connections (the resolver runs in its own request transaction). Callers
    are responsible for deleting the agent afterwards.
    """
    conn = real_connect(TEST_DATABASE_URL)
    try:
        perms = PermissionRepository(conn)
        agents = AgentRepository(conn)
        role = perms.create_role(f"writer-{uuid.uuid4()}")
        cap = perms.create_capability("write")
        perms.assign_capability_to_role(role["id"], cap["id"])
        agent = agents.create(
            owner=f"owner-{uuid.uuid4()}",
            purpose="security-test",
            primary_role_id=role["id"],
        )
        conn.commit()
        return str(agent["id"])
    finally:
        conn.close()


def install_write_resolver(actor_id: str) -> None:
    """Install a resolver that always returns a trusted writer context."""

    def _resolver(request):  # request intentionally unused; server-side trust
        return SecurityContext(
            actor_type="agent",
            actor_id=actor_id,
            authenticated=True,
            source="external",
        )

    set_security_context_resolver(_resolver)


def delete_agent(agent_id: str) -> None:
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM audit_logs WHERE actor_id = %s", (str(agent_id),)
            )
            cur.execute("DELETE FROM agents WHERE id = %s", (str(agent_id),))
        conn.commit()
    finally:
        conn.close()


def auth_client(monkeypatch, *, caps=("write",)):
    """Build a TestClient authenticated as a committed agent with ``caps``.

    The returned client redirects DB connections to the isolated test DB and
    installs a security-context resolver that always returns the seeded writer
    agent. The writer agent is committed up-front and returned alongside the
    client so the caller can clean it up (and so its id is available for
    assertions). Skips cleanly when ``TEST_DATABASE_URL`` is unset.

    This mirrors how a future auth mechanism would resolve a real, capability
    bearing principal — without inventing any concrete credential exchange.
    """
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set; skipping live security tests")

    conn = real_connect(TEST_DATABASE_URL)
    try:
        perms = PermissionRepository(conn)
        agents = AgentRepository(conn)
        role = perms.create_role(f"writer-{uuid.uuid4()}")
        for cap_key in caps:
            cap = perms.create_capability(cap_key)
            perms.assign_capability_to_role(role["id"], cap["id"])
        agent = agents.create(
            owner=f"owner-{uuid.uuid4()}",
            purpose="security-test",
            primary_role_id=role["id"],
        )
        conn.commit()
        agent_id = str(agent["id"])
    finally:
        conn.close()

    monkeypatch.setattr(
        "app.api.dependencies.connect",
        lambda url=None: real_connect(TEST_DATABASE_URL),
    )
    install_write_resolver(agent_id)

    from fastapi.testclient import TestClient
    from app.main import app

    return app, agent_id  # caller opens TestClient(app) itself

