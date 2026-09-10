"""Phase 5.9 tests for the Tool HTTP API.

Exercises the thin transport boundary over :class:`ToolService` through the
real FastAPI app and :class:`fastapi.testclient.TestClient`. Every test that
touches the database targets the isolated ``mind_test_db`` via
``TEST_DATABASE_URL`` (the project's sanctioned test database) by redirecting
the API's connection dependency; tests skip cleanly when that URL is unset.

The router must remain a transport adapter: it calls ``ToolService`` and
nothing else (no SQL, no repository, no connection management, no commit/
rollback). Those invariants are asserted directly.
"""
from __future__ import annotations

import inspect
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import connect as real_connect
from app.api.routers import tools as tools_router
from app.main import app
from tests.security.helpers import delete_agent

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def client(monkeypatch):
    """Authenticated TestClient (write-capable principal) over the test DB."""
    from tests.security.helpers import auth_client

    app, _agent_id = auth_client(monkeypatch)
    with TestClient(app) as c:
        yield c


def _cleanup_tool(tool_id: str) -> None:
    """Remove a committed tool and its audit rows (test hygiene only)."""
    if tool_id is None:
        return
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM audit_logs WHERE target_id = %s", (str(tool_id),)
            )
            cur.execute("DELETE FROM tools WHERE id = %s", (str(tool_id),))
        conn.commit()
    finally:
        conn.close()


# ── Architecture guards ─────────────────────────────────────
class TestRouterArchitecture:
    def test_no_sql_in_router(self):
        source = inspect.getsource(tools_router)
        assert "SELECT" not in source
        assert "INSERT" not in source
        assert "UPDATE" not in source
        assert "DELETE" not in source

    def test_no_direct_connection_in_router(self):
        source = inspect.getsource(tools_router)
        assert "psycopg.connect" not in source
        assert "connect(" not in source

    def test_no_authorization_duplicated_in_router(self):
        source = inspect.getsource(tools_router)
        assert "require_capability" not in source
        assert "AuthorizationError" not in source

    def test_no_audit_duplicated_in_router(self):
        source = inspect.getsource(tools_router)
        assert "audit.append" not in source
        assert "_audit." not in source
        assert "AuditService(" not in source

    def test_route_registration(self):
        paths = {route.path for route in tools_router.router.routes}
        assert "/tools" in paths
        assert "/tools/{tool_id}" in paths
        assert "/tools/{tool_id}/status" in paths
        assert "/tools/{tool_id}/verified" in paths
        assert "/tools/destructive" in paths
        assert "/tools/by-capability/{capability_key}" in paths

    def test_registered_exactly_once_in_app(self):
        if not TEST_DATABASE_URL:
            pytest.skip("TEST_DATABASE_URL not set")
        from app.main import app

        tool_paths = set()
        for route in app.routes:
            if hasattr(route, "path") and route.path.startswith("/tools"):
                tool_paths.add(route.path)
        assert "/tools" in tool_paths
        assert "/tools/destructive" in tool_paths


# ── Authentication ──────────────────────────────────────────
class TestAuthentication:
    def test_unauthenticated_create_tool(self, monkeypatch):
        monkeypatch.setattr(
            "app.api.dependencies.connect",
            lambda url=None: real_connect(TEST_DATABASE_URL),
        )
        with TestClient(app) as c:
            resp = c.post("/tools", json={"name": "x"})
        assert resp.status_code == 401

    def test_unauthenticated_update_status(self, monkeypatch):
        monkeypatch.setattr(
            "app.api.dependencies.connect",
            lambda url=None: real_connect(TEST_DATABASE_URL),
        )
        with TestClient(app) as c:
            resp = c.patch("/tools/00000000-0000-0000-0000-000000000000/status", json={"status": "x"})
        assert resp.status_code == 401

    def test_unauthenticated_mark_verified(self, monkeypatch):
        monkeypatch.setattr(
            "app.api.dependencies.connect",
            lambda url=None: real_connect(TEST_DATABASE_URL),
        )
        with TestClient(app) as c:
            resp = c.patch("/tools/00000000-0000-0000-0000-000000000000/verified", json={"is_verified": True})
        assert resp.status_code == 401

    def test_read_routes_require_no_auth(self, monkeypatch):
        if not TEST_DATABASE_URL:
            pytest.skip("TEST_DATABASE_URL not set")
        monkeypatch.setattr(
            "app.api.dependencies.connect",
            lambda url=None: real_connect(TEST_DATABASE_URL),
        )
        with TestClient(app) as c:
            assert c.get("/tools/destructive").status_code == 200
            assert c.get("/tools/by-capability/shell").status_code == 200


# ── Authorization ───────────────────────────────────────────
class TestAuthorization:
    def test_create_requires_write(self, client):
        resp = client.post("/tools", json={"name": "x"})
        assert resp.status_code == 403

    def test_update_status_requires_write(self, client):
        resp = client.patch(
            "/tools/00000000-0000-0000-0000-000000000000/status",
            json={"status": "active"},
        )
        assert resp.status_code == 403

    def test_mark_verified_requires_write(self, client):
        resp = client.patch(
            "/tools/00000000-0000-0000-0000-000000000000/verified",
            json={"is_verified": True},
        )
        assert resp.status_code == 403


# ── POST /tools ─────────────────────────────────────────────
class TestCreateTool:
    def test_success(self, client):
        resp = client.post(
            "/tools",
            json={
                "name": "rm",
                "description": "remove files",
                "version": "1.0",
                "requires_capability": "shell",
                "is_destructive": True,
                "is_verified": False,
                "config": {"path": "/tmp"},
                "status": "active",
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "rm"
        assert body["description"] == "remove files"
        assert body["version"] == "1.0"
        assert body["requires_capability"] == "shell"
        assert body["is_destructive"] is True
        assert body["is_verified"] is False
        assert body["status"] == "active"
        assert body["id"]
        _cleanup_tool(body["id"])

    def test_minimal_fields(self, client):
        resp = client.post("/tools", json={"name": "safe-tool"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "safe-tool"
        assert body["status"] == "active"
        assert body["is_destructive"] is False
        _cleanup_tool(body["id"])

    def test_validation_missing_name(self, client):
        resp = client.post("/tools", json={})
        assert resp.status_code == 422

    def test_validation_invalid_status(self, client):
        resp = client.post("/tools", json={"name": "x", "status": "weird"})
        assert resp.status_code == 400

    def test_validation_invalid_config(self, client):
        resp = client.post("/tools", json={"name": "x", "config": [1, 2]})
        assert resp.status_code == 400


# ── GET /tools/{tool_id} ─────────────────────────────────────
class TestGetTool:
    def test_success(self, client):
        created = client.post("/tools", json={"name": "get-me"}).json()
        resp = client.get(f"/tools/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == created["id"]
        _cleanup_tool(created["id"])

    def test_not_found(self, client):
        resp = client.get(f"/tools/{uuid.uuid4()}")
        assert resp.status_code == 404
        assert resp.json()["error"] == "not_found"

    def test_malformed_uuid(self, client):
        resp = client.get("/tools/not-a-uuid")
        assert resp.status_code == 400


# ── PATCH /tools/{tool_id}/status ───────────────────────────
class TestUpdateToolStatus:
    def test_success(self, client):
        created = client.post("/tools", json={"name": "status-tool"}).json()
        resp = client.patch(
            f"/tools/{created['id']}/status",
            json={"status": "disabled"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "disabled"
        _cleanup_tool(created["id"])

    def test_not_found(self, client):
        resp = client.patch(
            f"/tools/{uuid.uuid4()}/status",
            json={"status": "disabled"},
        )
        assert resp.status_code == 404

    def test_validation_bad_status(self, client):
        created = client.post("/tools", json={"name": "bad-status"}).json()
        resp = client.patch(
            f"/tools/{created['id']}/status",
            json={"status": "bogus"},
        )
        assert resp.status_code == 400
        _cleanup_tool(created["id"])


# ── PATCH /tools/{tool_id}/verified ─────────────────────────
class TestMarkToolVerified:
    def test_verify_true(self, client):
        created = client.post("/tools", json={"name": "verify-tool"}).json()
        resp = client.patch(
            f"/tools/{created['id']}/verified",
            json={"is_verified": True},
        )
        assert resp.status_code == 200
        assert resp.json()["is_verified"] is True
        _cleanup_tool(created["id"])

    def test_verify_false(self, client):
        created = client.post("/tools", json={"name": "unverify-tool"}).json()
        resp = client.patch(
            f"/tools/{created['id']}/verified",
            json={"is_verified": False},
        )
        assert resp.status_code == 200
        assert resp.json()["is_verified"] is False
        _cleanup_tool(created["id"])

    def test_not_found(self, client):
        resp = client.patch(
            f"/tools/{uuid.uuid4()}/verified",
            json={"is_verified": True},
        )
        assert resp.status_code == 404

    def test_validation_bad_type(self, client):
        created = client.post("/tools", json={"name": "bad-verify"}).json()
        resp = client.patch(
            f"/tools/{created['id']}/verified",
            json={"is_verified": "yes"},
        )
        assert resp.status_code == 400
        _cleanup_tool(created["id"])


# ── GET /tools/destructive ──────────────────────────────────
class TestListDestructive:
    def test_lists_only_destructive(self, client):
        d = client.post(
            "/tools",
            json={"name": "destructive-tool", "is_destructive": True},
        ).json()
        client.post(
            "/tools",
            json={"name": "safe-tool", "is_destructive": False},
        ).json()
        resp = client.get("/tools/destructive")
        assert resp.status_code == 200
        names = {t["name"] for t in resp.json()}
        assert "destructive-tool" in names
        assert "safe-tool" not in names
        _cleanup_tool(d["id"])

    def test_pagination(self, client):
        for i in range(3):
            client.post(
                "/tools",
                json={"name": f"dest-{i}", "is_destructive": True},
            ).json()
        resp = client.get("/tools/destructive", params={"limit": 2, "offset": 0})
        assert resp.status_code == 200
        assert len(resp.json()) == 2


# ── GET /tools/by-capability/{capability_key} ───────────────
class TestListByCapability:
    def test_filters_by_capability(self, client):
        shell = client.post(
            "/tools",
            json={"name": "shell-tool", "requires_capability": "shell"},
        ).json()
        client.post(
            "/tools",
            json={"name": "plain-tool", "requires_capability": None},
        ).json()
        resp = client.get("/tools/by-capability/shell")
        assert resp.status_code == 200
        names = {t["name"] for t in resp.json()}
        assert "shell-tool" in names
        assert "plain-tool" not in names
        _cleanup_tool(shell["id"])

    def test_pagination(self, client):
        for i in range(3):
            client.post(
                "/tools",
                json={"name": f"cap-tool-{i}", "requires_capability": "shell"},
            ).json()
        resp = client.get(
            "/tools/by-capability/shell", params={"limit": 2, "offset": 0}
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 2


# ── Security: denied audit ──────────────────────────────────
class TestDeniedAudit:
    def test_denied_create_audited(self, monkeypatch):
        if not TEST_DATABASE_URL:
            pytest.skip("TEST_DATABASE_URL not set")
        from app.api.security import SecurityContext, set_security_context_resolver

        conn = real_connect(TEST_DATABASE_URL)
        try:
            from app.repositories.agents import AgentRepository

            anon_agent = AgentRepository(conn).create(
                owner=f"anon-{uuid.uuid4()}", purpose="anon"
            )
            conn.commit()
            anon_id = str(anon_agent["id"])
        finally:
            conn.close()

        def _anon_resolver(request):
            return SecurityContext(
                actor_type="agent",
                actor_id=anon_id,
                authenticated=True,
                source="external",
            )

        set_security_context_resolver(_anon_resolver)
        try:
            monkeypatch.setattr(
                "app.api.dependencies.connect",
                lambda url=None: real_connect(TEST_DATABASE_URL),
            )
            with TestClient(app) as c:
                resp = c.post("/tools", json={"name": "audit-test"})
            assert resp.status_code == 403
        finally:
            set_security_context_resolver(
                app.router.on_startup[0] if app.router.on_startup else None
            )

        conn = real_connect(TEST_DATABASE_URL)
        try:
            from app.repositories.audit import AuditRepository

            audit_repo = AuditRepository(conn)
            records = audit_repo.list_by_target("tool", anon_id)
            denied = [
                r
                for r in records
                if r["action"] == "tool.create"
                and r["outcome"] == "denied"
                and r["capability_used"] == "write"
            ]
            assert len(denied) >= 1
        finally:
            conn.close()
            delete_agent(anon_id)
