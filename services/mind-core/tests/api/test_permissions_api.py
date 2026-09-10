"""Phase 5.8 API tests for the Permissions HTTP API.

Requires ``TEST_DATABASE_URL`` and skips cleanly when unset.

Tests the five Phase 5.8 endpoints against the real FastAPI app,
verifying admin:roles enforcement, trusted SecurityContext identity,
audit records, and error handling.
"""
from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import connect as real_connect
from app.api.security import set_security_context_resolver
from app.main import app
from tests.security.helpers import delete_agent, _seeded_writer_conn

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def _make_anon_agent() -> str:
    """Create a committed agent with no capabilities."""
    conn = real_connect(TEST_DATABASE_URL)
    try:
        from app.repositories.agents import AgentRepository

        agent = AgentRepository(conn).create(
            owner=f"owner-{uuid.uuid4()}", purpose="perms-test"
        )
        conn.commit()
        return str(agent["id"])
    finally:
        conn.close()


def _make_admin_agent() -> str:
    """Create a committed agent with admin:roles capability."""
    conn = real_connect(TEST_DATABASE_URL)
    try:
        from app.repositories.agents import AgentRepository
        from app.repositories.permissions import PermissionRepository

        perms = PermissionRepository(conn)
        agents = AgentRepository(conn)
        role = perms.create_role(f"admin-{uuid.uuid4()}")
        cap = perms.create_capability("admin:roles")
        perms.assign_capability_to_role(role["id"], cap["id"])
        agent = agents.create(
            owner=f"owner-{uuid.uuid4()}",
            purpose="perms-test",
            primary_role_id=role["id"],
        )
        conn.commit()
        return str(agent["id"])
    finally:
        conn.close()


def _install_resolver(actor_id: str) -> None:
    from app.api.security import SecurityContext

    def _resolver(request):
        return SecurityContext(
            actor_type="agent",
            actor_id=actor_id,
            authenticated=True,
            source="external",
        )

    set_security_context_resolver(_resolver)


@pytest.fixture
def client_no_auth(monkeypatch):
    """TestClient with no resolver (default deny-all)."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setattr(
        "app.api.dependencies.connect",
        lambda url=None: real_connect(TEST_DATABASE_URL),
    )
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_admin(monkeypatch):
    """TestClient authenticated as an admin:roles agent."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    agent_id = _make_admin_agent()
    try:
        monkeypatch.setattr(
            "app.api.dependencies.connect",
            lambda url=None: real_connect(TEST_DATABASE_URL),
        )
        _install_resolver(agent_id)
        with TestClient(app) as c:
            yield c
    finally:
        delete_agent(agent_id)


@pytest.fixture
def client_anon(monkeypatch):
    """TestClient authenticated as an agent without admin:roles."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    agent_id = _make_anon_agent()
    try:
        monkeypatch.setattr(
            "app.api.dependencies.connect",
            lambda url=None: real_connect(TEST_DATABASE_URL),
        )
        _install_resolver(agent_id)
        with TestClient(app) as c:
            yield c
    finally:
        delete_agent(agent_id)


@pytest.fixture(autouse=True)
def _reset_resolver():
    original = set_security_context_resolver.__globals__["_resolver"]
    try:
        yield
    finally:
        set_security_context_resolver(original)


def _cleanup_grant(conn, grant_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM permission_grants WHERE id = %s", (grant_id,))
    conn.commit()


# ── A. unauthenticated -> 401 ─────────────────────────────────────────────
class TestUnauthenticated:
    def test_grant_requires_auth(self, client_no_auth):
        resp = client_no_auth.post(
            "/permissions/grants",
            json={
                "agent_id": str(uuid.uuid4()),
                "capability_id": str(uuid.uuid4()),
            },
        )
        assert resp.status_code == 401

    def test_revoke_requires_auth(self, client_no_auth):
        resp = client_no_auth.post(
            "/permissions/revoke/00000000-0000-0000-0000-000000000000"
        )
        assert resp.status_code == 401

    def test_list_grants_requires_auth(self, client_no_auth):
        resp = client_no_auth.get(
            f"/permissions/agents/{uuid.uuid4()}/grants"
        )
        assert resp.status_code == 401

    def test_list_capabilities_requires_auth(self, client_no_auth):
        resp = client_no_auth.get("/permissions/capabilities")
        assert resp.status_code == 401

    def test_effective_requires_auth(self, client_no_auth):
        resp = client_no_auth.get(
            f"/permissions/agents/{uuid.uuid4()}/effective"
        )
        assert resp.status_code == 401


# ── B. authenticated without admin:roles -> 403 ───────────────────────────
class TestForbiddenWithoutAdmin:
    def test_grant_forbidden(self, client_anon):
        resp = client_anon.post(
            "/permissions/grants",
            json={
                "agent_id": str(uuid.uuid4()),
                "capability_id": str(uuid.uuid4()),
            },
        )
        assert resp.status_code == 403

    def test_revoke_forbidden(self, client_anon):
        resp = client_anon.post(
            "/permissions/revoke/00000000-0000-0000-0000-000000000000"
        )
        assert resp.status_code == 403

    def test_list_grants_forbidden(self, client_anon):
        resp = client_anon.get(
            f"/permissions/agents/{uuid.uuid4()}/grants"
        )
        assert resp.status_code == 403

    def test_list_capabilities_forbidden(self, client_anon):
        resp = client_anon.get("/permissions/capabilities")
        assert resp.status_code == 403

    def test_effective_forbidden(self, client_anon):
        resp = client_anon.get(
            f"/permissions/agents/{uuid.uuid4()}/effective"
        )
        assert resp.status_code == 403


# ── C. POST /permissions/grants ───────────────────────────────────────────
class TestGrantCapability:
    def test_grant_success(self, client_admin):
        target_id = _make_anon_agent()
        try:
            cap_resp = client_admin.get("/permissions/capabilities?limit=200")
            assert cap_resp.status_code == 200
            caps = cap_resp.json()
            write_cap = next(c for c in caps if c["key"] == "write")
            cap_id = write_cap["id"]

            resp = client_admin.post(
                "/permissions/grants",
                json={
                    "agent_id": target_id,
                    "capability_id": cap_id,
                    "scope": "test-scope",
                },
            )
            assert resp.status_code == 201
            body = resp.json()
            assert body["agent_id"] == target_id
            assert body["capability_id"] == cap_id
            assert body["scope"] == "test-scope"
            assert body["granted_by"] == client_admin
        finally:
            delete_agent(target_id)

    def test_grant_uses_trusted_actor_as_granted_by(self, client_admin):
        target_id = _make_anon_agent()
        try:
            cap_resp = client_admin.get("/permissions/capabilities?limit=200")
            caps = cap_resp.json()
            write_cap = next(c for c in caps if c["key"] == "write")
            cap_id = write_cap["id"]

            resp = client_admin.post(
                "/permissions/grants",
                json={
                    "agent_id": target_id,
                    "capability_id": cap_id,
                },
            )
            assert resp.status_code == 201
            granted_by = resp.json()["granted_by"]
            assert granted_by is not None
        finally:
            delete_agent(target_id)

    def test_grant_nonexistent_agent_returns_404(self, client_admin):
        cap_resp = client_admin.get("/permissions/capabilities?limit=200")
        caps = cap_resp.json()
        write_cap = next(c for c in caps if c["key"] == "write")
        cap_id = write_cap["id"]

        resp = client_admin.post(
            "/permissions/grants",
            json={
                "agent_id": str(uuid.uuid4()),
                "capability_id": cap_id,
            },
        )
        assert resp.status_code == 404

    def test_grant_nonexistent_capability_returns_404(self, client_admin):
        target_id = _make_anon_agent()
        try:
            resp = client_admin.post(
                "/permissions/grants",
                json={
                    "agent_id": target_id,
                    "capability_id": str(uuid.uuid4()),
                },
            )
            assert resp.status_code == 404
        finally:
            delete_agent(target_id)

    def test_grant_denied_is_audited(self, client_anon):
        target_id = _make_anon_agent()
        try:
            resp = client_anon.post(
                "/permissions/grants",
                json={
                    "agent_id": target_id,
                    "capability_id": str(uuid.uuid4()),
                },
            )
            assert resp.status_code == 403

            conn = real_connect(TEST_DATABASE_URL)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT action, outcome, capability_used, actor_id "
                        "FROM audit_logs WHERE action = 'permission.grant' "
                        "ORDER BY created_at DESC LIMIT 1"
                    )
                    row = cur.fetchone()
            finally:
                conn.close()
            assert row is not None
            assert row[1] == "denied"
            assert row[2] == "admin:roles"
        finally:
            delete_agent(target_id)


# ── D. POST /permissions/revoke/{grant_id} ────────────────────────────────
class TestRevokeCapability:
    def test_revoke_success(self, client_admin):
        target_id = _make_anon_agent()
        try:
            cap_resp = client_admin.get("/permissions/capabilities?limit=200")
            caps = cap_resp.json()
            write_cap = next(c for c in caps if c["key"] == "write")
            cap_id = write_cap["id"]

            created = client_admin.post(
                "/permissions/grants",
                json={
                    "agent_id": target_id,
                    "capability_id": cap_id,
                },
            ).json()
            grant_id = created["id"]

            resp = client_admin.post(f"/permissions/revoke/{grant_id}")
            assert resp.status_code == 200
            body = resp.json()
            assert body["id"] == grant_id
            assert body["revoked_at"] is not None
        finally:
            delete_agent(target_id)

    def test_revoke_nonexistent_grant_returns_404(self, client_admin):
        resp = client_admin.post(
            "/permissions/revoke/00000000-0000-0000-0000-000000000000"
        )
        assert resp.status_code == 404

    def test_revoke_denied_is_audited(self, client_anon):
        resp = client_anon.post(
            "/permissions/revoke/00000000-0000-0000-0000-000000000000"
        )
        assert resp.status_code == 403

        conn = real_connect(TEST_DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT action, outcome, capability_used, actor_id "
                    "FROM audit_logs WHERE action = 'permission.revoke' "
                    "ORDER BY created_at DESC LIMIT 1"
                )
                row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[1] == "denied"
        assert row[2] == "admin:roles"


# ── E. GET /permissions/agents/{agent_id}/grants ───────────────────────────
class TestListGrantsForAgent:
    def test_list_grants_success(self, client_admin):
        target_id = _make_anon_agent()
        try:
            cap_resp = client_admin.get("/permissions/capabilities?limit=200")
            caps = cap_resp.json()
            write_cap = next(c for c in caps if c["key"] == "write")
            cap_id = write_cap["id"]

            client_admin.post(
                "/permissions/grants",
                json={
                    "agent_id": target_id,
                    "capability_id": cap_id,
                },
            )

            resp = client_admin.get(
                f"/permissions/agents/{target_id}/grants"
            )
            assert resp.status_code == 200
            rows = resp.json()
            assert len(rows) >= 1
            assert rows[0]["agent_id"] == target_id
            assert rows[0]["capability_id"] == cap_id
        finally:
            delete_agent(target_id)

    def test_list_grants_nonexistent_agent_returns_404(self, client_admin):
        resp = client_admin.get(
            f"/permissions/agents/{uuid.uuid4()}/grants"
        )
        assert resp.status_code == 404

    def test_list_grants_honors_pagination(self, client_admin):
        target_id = _make_anon_agent()
        try:
            cap_resp = client_admin.get("/permissions/capabilities?limit=200")
            caps = cap_resp.json()
            read_cap = next(c for c in caps if c["key"] == "read")
            cap_id = read_cap["id"]

            for _ in range(3):
                client_admin.post(
                    "/permissions/grants",
                    json={
                        "agent_id": target_id,
                        "capability_id": cap_id,
                    },
                )

            resp = client_admin.get(
                f"/permissions/agents/{target_id}/grants",
                params={"limit": 2, "offset": 0},
            )
            assert resp.status_code == 200
            assert len(resp.json()) == 2
        finally:
            delete_agent(target_id)


# ── F. GET /permissions/capabilities ──────────────────────────────────────
class TestListCapabilities:
    def test_list_capabilities_success(self, client_admin):
        resp = client_admin.get("/permissions/capabilities")
        assert resp.status_code == 200
        rows = resp.json()
        assert isinstance(rows, list)
        assert len(rows) >= 1
        for row in rows:
            assert "id" in row
            assert "key" in row
            assert "description" in row
            assert len(row) == 3

    def test_list_capabilities_narrow_schema(self, client_admin):
        resp = client_admin.get("/permissions/capabilities")
        assert resp.status_code == 200
        for row in resp.json():
            extra = set(row.keys()) - {"id", "key", "description"}
            assert extra == set(), f"Unexpected columns exposed: {extra}"

    def test_list_capabilities_honors_pagination(self, client_admin):
        resp = client_admin.get(
            "/permissions/capabilities", params={"limit": 1, "offset": 0}
        )
        assert resp.status_code == 200
        assert len(resp.json()) <= 1


# ── G. GET /permissions/agents/{agent_id}/effective ────────────────────────
class TestEffectiveCapabilities:
    def test_effective_success(self, client_admin):
        target_id = _make_anon_agent()
        try:
            cap_resp = client_admin.get("/permissions/capabilities?limit=200")
            caps = cap_resp.json()
            write_cap = next(c for c in caps if c["key"] == "write")
            cap_id = write_cap["id"]

            client_admin.post(
                "/permissions/grants",
                json={
                    "agent_id": target_id,
                    "capability_id": cap_id,
                },
            )

            resp = client_admin.get(
                f"/permissions/agents/{target_id}/effective"
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["agent_id"] == target_id
            keys = {c["key"] for c in body["capabilities"]}
            assert "write" in keys
            for entry in body["capabilities"]:
                assert "key" in entry
                assert "source" in entry
                assert entry["source"] in ("role", "grant")
                assert len(entry) == 2
        finally:
            delete_agent(target_id)

    def test_effective_nonexistent_agent_returns_404(self, client_admin):
        resp = client_admin.get(
            f"/permissions/agents/{uuid.uuid4()}/effective"
        )
        assert resp.status_code == 404


# ── H. route registration ─────────────────────────────────────────────────
class TestRouteRegistration:
    def test_all_routes_registered(self):
        from app.api.routers import permissions as perms_router

        paths = {route.path for route in perms_router.router.routes}
        assert "/permissions/grants" in paths
        assert "/permissions/revoke/{grant_id}" in paths
        assert "/permissions/agents/{agent_id}/grants" in paths
        assert "/permissions/capabilities" in paths
        assert "/permissions/agents/{agent_id}/effective" in paths
