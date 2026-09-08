"""Phase 5.2 tests for the Agent HTTP API.

Exercises the thin transport boundary over :class:`AgentService` through the
real FastAPI app and :class:`fastapi.testclient.TestClient`. Every test that
touches the database targets the isolated ``mind_test_db`` via
``TEST_DATABASE_URL`` (the project's sanctioned test database) by redirecting
the API's connection dependency; tests skip cleanly when that URL is unset.

The router must remain a transport adapter: it calls ``AgentService`` and
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
from app.main import app
from app.services.agent_service import AgentService

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def client(monkeypatch):
    """Authenticated TestClient (write-capable principal) over the test DB."""
    from tests.security.helpers import auth_client

    app, _agent_id = auth_client(monkeypatch)
    with TestClient(app) as c:
        yield c


def _cleanup_agent(agent_id: str) -> None:
    """Remove a committed agent and its audit rows (test hygiene only)."""
    if agent_id is None:
        return
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM audit_logs WHERE target_id = %s", (str(agent_id),)
            )
            cur.execute("DELETE FROM agents WHERE id = %s", (str(agent_id),))
        conn.commit()
    finally:
        conn.close()


def _cleanup_by_owner(owner: str) -> None:
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM agents WHERE owner = %s", (owner,)
            )
            ids = [r[0] for r in cur.fetchall()]
        for aid in ids:
            _cleanup_agent(str(aid))
    finally:
        conn.close()


def _make_role() -> str:
    """Create a throwaway role directly for list-by-role setup."""
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO roles (name) VALUES (%s) RETURNING id",
                (f"role-{uuid.uuid4()}",),
            )
            rid = cur.fetchone()[0]
        conn.commit()
        return str(rid)
    finally:
        conn.close()


def _delete_role(role_id: str) -> None:
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM roles WHERE id = %s", (role_id,))
        conn.commit()
    finally:
        conn.close()


# ── 1. POST /agents success ───────────────────────────────
def test_post_agent_success(client):
    owner = f"owner-{uuid.uuid4()}"
    actor = f"actor-{uuid.uuid4()}"
    resp = client.post(
        "/agents",
        json={"owner": owner, "purpose": "api create"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["owner"] == owner
    assert body["status"] == "active"
    assert body["autonomy_level"] == 0
    assert body["id"]
    _cleanup_agent(body["id"])


# ── 2. POST /agents validation failure ────────────────────
def test_post_agent_validation_failure(client):
    owner = f"owner-{uuid.uuid4()}"
    actor = f"actor-{uuid.uuid4()}"
    # Invalid status must be rejected by the service -> 400.
    resp = client.post(
        "/agents",
        json={
            "owner": owner,
            "purpose": "bad status",
            "status": "bogus",
            
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


# ── 3. POST /agents missing required field ────────────────
def test_post_agent_missing_required_field(client):
    # Missing `owner` -> Pydantic transport validation -> 422.
    resp = client.post(
        "/agents", json={"purpose": "no owner", "actor_id": "x"}
    )
    assert resp.status_code == 422


# ── 4. GET /agents/{id} success ───────────────────────────
def test_get_agent_success(client):
    owner = f"owner-{uuid.uuid4()}"
    actor = f"actor-{uuid.uuid4()}"
    created = client.post(
        "/agents", json={"owner": owner, "purpose": "get"}
    ).json()
    resp = client.get(f"/agents/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]
    _cleanup_agent(created["id"])


# ── 5. GET /agents/{id} nonexistent → 404 ────────────────
def test_get_agent_not_found(client):
    missing = str(uuid.uuid4())
    resp = client.get(f"/agents/{missing}")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


# ── 6. GET /agents/{id} malformed UUID → validation ──────
def test_get_agent_malformed_uuid(client):
    # Path param is str; the service validates UUID shape -> 400.
    resp = client.get("/agents/not-a-uuid")
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


# ── 7. PATCH status success ───────────────────────────────
def test_patch_status_success(client):
    owner = f"owner-{uuid.uuid4()}"
    actor = f"actor-{uuid.uuid4()}"
    created = client.post(
        "/agents", json={"owner": owner, "purpose": "patch"}
    ).json()
    resp = client.patch(
        f"/agents/{created['id']}/status",
        json={"status": "suspended"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "suspended"
    _cleanup_agent(created["id"])


# ── 8. PATCH status invalid value ─────────────────────────
def test_patch_status_invalid(client):
    owner = f"owner-{uuid.uuid4()}"
    actor = f"actor-{uuid.uuid4()}"
    created = client.post(
        "/agents", json={"owner": owner, "purpose": "patch"}
    ).json()
    resp = client.patch(
        f"/agents/{created['id']}/status",
        json={"status": "nope"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"
    _cleanup_agent(created["id"])


# ── 9. PATCH autonomy success ─────────────────────────────
def test_patch_autonomy_success(client):
    owner = f"owner-{uuid.uuid4()}"
    actor = f"actor-{uuid.uuid4()}"
    created = client.post(
        "/agents", json={"owner": owner, "purpose": "patch"}
    ).json()
    resp = client.patch(
        f"/agents/{created['id']}/autonomy-level",
        json={"autonomy_level": 2},
    )
    assert resp.status_code == 200
    assert resp.json()["autonomy_level"] == 2
    _cleanup_agent(created["id"])


# ── 10. PATCH autonomy invalid value ──────────────────────
def test_patch_autonomy_invalid(client):
    owner = f"owner-{uuid.uuid4()}"
    actor = f"actor-{uuid.uuid4()}"
    created = client.post(
        "/agents", json={"owner": owner, "purpose": "patch"}
    ).json()
    resp = client.patch(
        f"/agents/{created['id']}/autonomy-level",
        json={"autonomy_level": 99},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"
    _cleanup_agent(created["id"])


# ── 11. GET by owner ──────────────────────────────────────
def test_get_by_owner(client):
    owner = f"owner-{uuid.uuid4()}"
    actor = f"actor-{uuid.uuid4()}"
    for _ in range(3):
        client.post(
            "/agents",
            json={"owner": owner, "purpose": "by-owner"},
        )
    resp = client.get(f"/agents/by-owner/{owner}")
    assert resp.status_code == 200
    assert len(resp.json()) == 3
    _cleanup_by_owner(owner)


# ── 12. GET by role ───────────────────────────────────────
def test_get_by_role(client):
    role_id = _make_role()
    try:
        actor = f"actor-{uuid.uuid4()}"
        for _ in range(2):
            client.post(
                "/agents",
                json={
                    "owner": f"owner-{uuid.uuid4()}",
                    "purpose": "by-role",
                    "primary_role_id": role_id,
                    
                },
            )
        resp = client.get(f"/agents/by-role/{role_id}")
        assert resp.status_code == 200
        assert len(resp.json()) == 2
        assert all(r["primary_role_id"] == role_id for r in resp.json())
    finally:
        _cleanup_by_owner("owner-%")  # no-op safety; explicit cleanup below
        # Remove agents created under this role.
        conn = real_connect(TEST_DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM agents WHERE primary_role_id = %s",
                    (role_id,),
                )
                for (aid,) in cur.fetchall():
                    _cleanup_agent(str(aid))
        finally:
            conn.close()
        _delete_role(role_id)


# ── 13. pagination limit ──────────────────────────────────
def test_pagination_limit(client):
    owner = f"owner-{uuid.uuid4()}"
    actor = f"actor-{uuid.uuid4()}"
    for _ in range(5):
        client.post(
            "/agents",
            json={"owner": owner, "purpose": "pag"},
        )
    resp = client.get(f"/agents/by-owner/{owner}", params={"limit": 2})
    assert resp.status_code == 200
    assert len(resp.json()) == 2
    _cleanup_by_owner(owner)

# ── 14. pagination offset ─────────────────────────────────
def test_pagination_offset(client):
    owner = f"owner-{uuid.uuid4()}"
    actor = f"actor-{uuid.uuid4()}"
    for _ in range(5):
        client.post(
            "/agents",
            json={"owner": owner, "purpose": "pag"},
        )
    first = client.get(
        f"/agents/by-owner/{owner}", params={"limit": 2, "offset": 0}
    ).json()
    second = client.get(
        f"/agents/by-owner/{owner}", params={"limit": 2, "offset": 2}
    ).json()
    assert len(second) == 2
    assert {r["id"] for r in first}.isdisjoint({r["id"] for r in second})
    _cleanup_by_owner(owner)


# ── 15. service error mapping (NotFoundError → 404) ───────
def test_service_error_mapping(client):
    resp = client.get(f"/agents/{uuid.uuid4()}")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


# ── 16. transaction commit on successful mutation ────────
def test_transaction_commit_on_success(client):
    owner = f"owner-{uuid.uuid4()}"
    actor = f"actor-{uuid.uuid4()}"
    resp = client.post(
        "/agents", json={"owner": owner, "purpose": "commit"}
    )
    assert resp.status_code == 201
    agent_id = resp.json()["id"]
    # Verify the insert was committed in the test DB.
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM agents WHERE id = %s", (agent_id,))
            assert cur.fetchone()[0] == 1
    finally:
        conn.close()
        _cleanup_agent(agent_id)


# ── 17. transaction rollback when domain error occurs ─────
def test_transaction_rollback_on_domain_error(client):
    owner = f"owner-{uuid.uuid4()}"
    actor = f"actor-{uuid.uuid4()}"
    # Invalid autonomy_level -> ValidationError -> rollback, no row committed.
    resp = client.post(
        "/agents",
        json={
            "owner": owner,
            "purpose": "rollback",
            "autonomy_level": 99,
            
        },
    )
    assert resp.status_code == 400
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM agents WHERE owner = %s "
                "AND purpose = 'rollback'",
                (owner,),
            )
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()


# ── 18. no unmanaged connection in router ─────────────────
def test_router_has_no_unmanaged_connection():
    import app.api.routers.agents as mod

    src = inspect.getsource(mod)
    # The router must not open its own connection or manage transactions.
    assert "psycopg.connect" not in src
    assert "conn.commit" not in src
    assert "conn.rollback" not in src
    # It must reuse the sanctioned dependencies.
    assert "get_txn" in src
    assert "get_conn" in src


# ── 19. router does not bypass AgentService ───────────────
def test_router_invokes_agent_service(monkeypatch, client):
    import app.api.routers.agents as mod

    # The router must not touch the repository layer directly.
    assert "AgentRepository" not in inspect.getsource(mod)

    called = {"create": False}
    original = AgentService.create

    def _spy(self, *args, **kwargs):
        called["create"] = True
        return original(self, *args, **kwargs)

    monkeypatch.setattr(AgentService, "create", _spy)
    owner = f"owner-{uuid.uuid4()}"
    actor = f"actor-{uuid.uuid4()}"
    resp = client.post(
        "/agents", json={"owner": owner, "purpose": "spy"}
    )
    assert resp.status_code == 201
    assert called["create"] is True
    _cleanup_agent(resp.json()["id"])


# ── 20. audit record exists after successful mutation ─────
def test_audit_record_after_success(client):
    owner = f"owner-{uuid.uuid4()}"
    actor = f"actor-{uuid.uuid4()}"
    resp = client.post(
        "/agents", json={"owner": owner, "purpose": "audit"}
    )
    assert resp.status_code == 201
    agent_id = resp.json()["id"]
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action, outcome, actor_id FROM audit_logs "
                "WHERE target_type = 'agent' AND target_id = %s",
                (agent_id,),
            )
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "agent.create"
        assert rows[0][1] == "success"
        assert rows[0][2] is not None
    finally:
        conn.close()
        _cleanup_agent(agent_id)


# ── 21. failed mutation leaves no audit/data rows ─────────
def test_no_rows_after_rollback(client):
    owner = f"owner-{uuid.uuid4()}"
    actor = f"actor-{uuid.uuid4()}"
    resp = client.post(
        "/agents",
        json={
            "owner": owner,
            "purpose": "no-rows",
            "autonomy_level": 99,
            
        },
    )
    assert resp.status_code == 400
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM agents WHERE purpose = 'no-rows'",
            )
            assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT COUNT(*) FROM audit_logs "
                "WHERE target_type = 'agent' AND action = 'agent.create' "
                "AND context::text LIKE '%no-rows%'"
            )
            # No audit row should reference a rolled-back creation.
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()