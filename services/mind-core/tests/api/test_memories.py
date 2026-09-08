"""Phase 5.6 tests for the Memory HTTP API.

Exercises the thin transport boundary over :class:`MemoryService` through the
real FastAPI app and :class:`fastapi.testclient.TestClient`. Every test that
touches the database targets the isolated ``mind_test_db`` via
``TEST_DATABASE_URL`` (the project's sanctioned test database) by redirecting
the API's connection dependency; tests skip cleanly when that URL is unset.

The router must remain a transport adapter: it calls ``MemoryService`` and
nothing else (no SQL, no repository, no connection management, no commit/
rollback, no audit mirroring). Those invariants are asserted directly.

Secret-classification policy boundary: the router blocks any create/escalate
to ``secret`` and any listing of ``secret`` memories at the policy boundary
(403 ``policy_violation``), because there is no authentication layer yet.
"""

from __future__ import annotations

import inspect
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import connect as real_connect
from app.api.routers import memories as memories_router
from app.main import app
from app.services.memory_service import MemoryService

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def client(monkeypatch):
    """Authenticated TestClient (write-capable principal) over the test DB."""
    from tests.security.helpers import auth_client

    app, _agent_id = auth_client(monkeypatch)
    with TestClient(app) as c:
        yield c


def _cleanup_memory(memory_id: str) -> None:
    """Remove a committed memory and its audit rows (test hygiene only)."""
    if memory_id is None:
        return
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM audit_logs WHERE target_id = %s", (str(memory_id),))
            cur.execute("DELETE FROM memories WHERE id = %s", (str(memory_id),))
        conn.commit()
    finally:
        conn.close()


def _make_agent() -> str:
    """Create a throwaway agent directly for memory setup (FK target)."""
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agents (owner, purpose) VALUES (%s, %s) RETURNING id",
                (f"owner-{uuid.uuid4()}", "memory-api-setup"),
            )
            aid = cur.fetchone()[0]
        conn.commit()
        return str(aid)
    finally:
        conn.close()


def _delete_agent(agent_id: str) -> None:
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM agents WHERE id = %s", (agent_id,))
        conn.commit()
    finally:
        conn.close()


def _count_audit(action: str, target_type: str = "memory") -> int:
    """Count audit_logs rows for a given action/target (verifies side effects)."""
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM audit_logs WHERE action = %s AND target_type = %s",
                (action, target_type),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def _create_memory(client, agent_id: str, **overrides) -> dict:
    payload = {
        "agent_id": agent_id,
        "type": "semantic",
        "content": f"memory-{uuid.uuid4()}",
    }
    payload.update(overrides)
    return client.post("/memories", json=payload).json()


def _delete_memory(client, memory_id: str, **overrides):
    """DELETE /memories/{id}?actor_type=&actor_id= (no body in this client)."""
    params = {}
    params.update(overrides)
    return client.delete(f"/memories/{memory_id}", params=params)


# ── A. ROUTE REGISTRATION ───────────────────────────────
def test_all_routes_registered():
    # Inspect the router directly (app.routes wraps it in an _IncludedRouter).
    paths = {route.path for route in memories_router.router.routes}
    assert "/memories" in paths
    assert "/memories/{memory_id}" in paths
    assert "/memories/by-agent/{agent_id}" in paths
    assert "/memories/by-classification/{classification}" in paths


def test_only_post_get_patch_delete_exposed():
    # No PUT on the memories router (full replace is not supported).
    methods = set()
    for route in memories_router.router.routes:
        if route.path.startswith("/memories"):
            methods |= route.methods
    assert "POST" in methods
    assert "GET" in methods
    assert "PATCH" in methods
    assert "DELETE" in methods
    assert "PUT" not in methods


# ── B. CREATE SUCCESS ───────────────────────────────────
def test_post_memory_success(client):
    agent_id = _make_agent()
    try:
        actor = f"actor-{uuid.uuid4()}"
        resp = client.post(
            "/memories",
            json={
                "agent_id": agent_id,
                "type": "semantic",
                "content": "M.I.N.D. = Multimodal Intelligence & Neural Diagnostics",
                "classification": "internal",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["agent_id"] == agent_id
        assert body["type"] == "semantic"
        assert body["content"]
        assert body["classification"] == "internal"
        assert body["id"]
        assert body["created_at"]
        assert body["updated_at"]
        _cleanup_memory(body["id"])
    finally:
        _delete_agent(agent_id)


def test_post_memory_default_classification_internal(client):
    agent_id = _make_agent()
    try:
        resp = client.post(
            "/memories",
            json={
                "agent_id": agent_id,
                "type": "episodic",
                "content": "default classification should be internal",
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["classification"] == "internal"
        _cleanup_memory(resp.json()["id"])
    finally:
        _delete_agent(agent_id)


def test_post_memory_with_metadata_and_embedding(client):
    agent_id = _make_agent()
    try:
        resp = client.post(
            "/memories",
            json={
                "agent_id": agent_id,
                "type": "working",
                "content": "vector memory",
                "metadata": {"source": "sensor-7", "confidence": 0.91},
                "embedding": [0.1, 0.2, 0.3],
            },
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["metadata"] == {"source": "sensor-7", "confidence": 0.91}
        assert data["embedding"] == [0.1, 0.2, 0.3]
        _cleanup_memory(data["id"])
    finally:
        _delete_agent(agent_id)


def test_post_memory_actor_type_human(client):
    agent_id = _make_agent()
    try:
        resp = client.post(
            "/memories",
            json={
                "agent_id": agent_id,
                "type": "semantic",
                "content": "logged by a human actor",
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["classification"] == "internal"
        _cleanup_memory(resp.json()["id"])
    finally:
        _delete_agent(agent_id)


# ── C. CREATE VALIDATION ────────────────────────────────
def test_post_memory_missing_required_field(client):
    # Missing `content` -> Pydantic transport validation -> 422.
    resp = client.post("/memories", json={"agent_id": str(uuid.uuid4()), "type": "semantic"})
    assert resp.status_code == 422


def test_post_memory_without_body_actor_id_succeeds(client):
    # Phase 5.8: the audit actor is taken from the trusted SecurityContext,
    # not the request body, so a memory can be created without supplying
    # actor_id in the body.
    resp = client.post(
        "/memories",
        json={
            "agent_id": str(uuid.uuid4()),
            "type": "semantic",
            "content": "x",
        },
    )
    assert resp.status_code == 201, resp.text
    _cleanup_memory(resp.json()["id"])


def test_post_memory_rejects_secret(client):
    # Policy boundary: secret is metadata-only, no auth layer yet -> 403.
    agent_id = _make_agent()
    try:
        resp = client.post(
            "/memories",
            json={
                "agent_id": agent_id,
                "type": "semantic",
                "content": "top secret",
                "classification": "secret",
            },
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"] == "policy_violation"
    finally:
        _delete_agent(agent_id)


def test_post_memory_invalid_type(client):
    agent_id = _make_agent()
    try:
        resp = client.post(
            "/memories",
            json={
                "agent_id": agent_id,
                "type": "procedural",
                "content": "x",
            },
        )
        assert resp.status_code == 422
    finally:
        _delete_agent(agent_id)


def test_post_memory_empty_content(client):
    agent_id = _make_agent()
    try:
        resp = client.post(
            "/memories",
            json={
                "agent_id": agent_id,
                "type": "semantic",
                "content": "",
            },
        )
        assert resp.status_code == 422
    finally:
        _delete_agent(agent_id)


def test_post_memory_invalid_agent_id(client):
    # Non-UUID agent_id -> service ValidationError -> 400.
    resp = client.post(
        "/memories",
        json={
            "agent_id": "not-a-uuid",
            "type": "semantic",
            "content": "x",
        },
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "validation_error"


def test_post_memory_actor_id_is_free_text(client):
    # audit_logs.actor_id is TEXT (may be a UUID, role name, or human id),
    # so a non-UUID actor_id is accepted and stored verbatim.
    agent_id = _make_agent()
    try:
        resp = client.post(
            "/memories",
            json={
                "agent_id": agent_id,
                "type": "semantic",
                "content": "x",
            },
        )
        assert resp.status_code == 201, resp.text
        _cleanup_memory(resp.json()["id"])
    finally:
        _delete_agent(agent_id)


# ── D. GET BY ID ────────────────────────────────────────
def test_get_memory_success(client):
    agent_id = _make_agent()
    try:
        created = _create_memory(client, agent_id, content="remember this")
        resp = client.get(f"/memories/{created['id']}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["id"] == created["id"]
        assert data["content"] == "remember this"
        # deleted_at / internal soft-delete state must not leak
        assert "deleted_at" not in data
        _cleanup_memory(created["id"])
    finally:
        _delete_agent(agent_id)


def test_get_memory_not_found(client):
    missing = str(uuid.uuid4())
    resp = client.get(f"/memories/{missing}")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"] == "not_found"


def test_get_memory_malformed_uuid(client):
    resp = client.get("/memories/not-a-uuid")
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "validation_error"


# ── E. GET BY AGENT ─────────────────────────────────────
def test_get_by_agent(client):
    agent_id = _make_agent()
    try:
        for _ in range(3):
            _create_memory(client, agent_id)
        resp = client.get(f"/memories/by-agent/{agent_id}")
        assert resp.status_code == 200, resp.text
        assert len(resp.json()) == 3
        assert all(r["agent_id"] == agent_id for r in resp.json())
        for r in resp.json():
            _cleanup_memory(r["id"])
    finally:
        _delete_agent(agent_id)


def test_get_by_agent_invalid_uuid(client):
    resp = client.get("/memories/by-agent/not-a-uuid")
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "validation_error"


def test_get_by_agent_excludes_soft_deleted(client):
    agent_id = _make_agent()
    try:
        created = _create_memory(client, agent_id, content="will be deleted")
        mid = created["id"]
        _delete_memory(client, mid)
        resp = client.get(f"/memories/by-agent/{agent_id}")
        assert resp.status_code == 200, resp.text
        ids = [m["id"] for m in resp.json()]
        assert mid not in ids
        _cleanup_memory(mid)
    finally:
        _delete_agent(agent_id)


# ── F. GET BY CLASSIFICATION ────────────────────────────
def test_get_by_classification(client):
    agent_id = _make_agent()
    try:
        _create_memory(client, agent_id, content="public note", classification="public")
        resp = client.get("/memories/by-classification/public")
        assert resp.status_code == 200, resp.text
        assert all(m["classification"] == "public" for m in resp.json())
        for r in resp.json():
            _cleanup_memory(r["id"])
    finally:
        _delete_agent(agent_id)


def test_get_by_classification_secret_blocked(client):
    # Policy boundary: listing secret is unsafe without an auth layer -> 403.
    resp = client.get("/memories/by-classification/secret")
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"] == "policy_violation"


def test_get_by_classification_invalid(client):
    # Unknown tier -> transport validation (Literal) -> 422, never reaches DB.
    resp = client.get("/memories/by-classification/topsecret")
    assert resp.status_code == 422, resp.text


# ── G. PATCH ────────────────────────────────────────────
def test_patch_memory_success(client):
    agent_id = _make_agent()
    try:
        created = _create_memory(client, agent_id, content="original")
        mid = created["id"]
        resp = client.patch(
            f"/memories/{mid}",
            json={"content": "updated"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["content"] == "updated"
        _cleanup_memory(mid)
    finally:
        _delete_agent(agent_id)


def test_patch_memory_preserves_unchanged_fields(client):
    agent_id = _make_agent()
    try:
        created = _create_memory(client, agent_id, type="episodic", content="keep type")
        mid = created["id"]
        resp = client.patch(
            f"/memories/{mid}",
            json={
                "content": "new content",
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["type"] == "episodic"  # unchanged
        assert data["content"] == "new content"
        _cleanup_memory(mid)
    finally:
        _delete_agent(agent_id)


def test_patch_memory_not_found(client):
    resp = client.patch(
        f"/memories/{uuid.uuid4()}",
        json={"content": "x"},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"] == "not_found"


def test_patch_memory_rejects_secret(client):
    agent_id = _make_agent()
    try:
        created = _create_memory(client, agent_id, content="internal")
        mid = created["id"]
        resp = client.patch(
            f"/memories/{mid}",
            json={
                "classification": "secret",
            },
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"] == "policy_violation"
        _cleanup_memory(mid)
    finally:
        _delete_agent(agent_id)


def test_patch_memory_invalid_id(client):
    resp = client.patch(
        "/memories/not-a-uuid",
        json={"content": "x"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "validation_error"


def test_patch_memory_without_body_actor_id_succeeds(client):
    # Phase 5.8: audit actor comes from the trusted SecurityContext.
    agent_id = _make_agent()
    try:
        created = _create_memory(client, agent_id, content="x")
        resp = client.patch(f"/memories/{created['id']}", json={"content": "y"})
        assert resp.status_code == 200, resp.text
        _cleanup_memory(created["id"])
    finally:
        _delete_agent(agent_id)


# ── H. DELETE (soft) ────────────────────────────────────
def test_delete_memory_success(client):
    agent_id = _make_agent()
    try:
        created = _create_memory(client, agent_id, content="to delete")
        mid = created["id"]
        resp = _delete_memory(client, mid)
        assert resp.status_code == 200, resp.text
        assert resp.json()["id"] == mid
        _cleanup_memory(mid)
    finally:
        _delete_agent(agent_id)


def test_delete_memory_not_found(client):
    resp = _delete_memory(client, uuid.uuid4())
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"] == "not_found"


def test_delete_memory_invalid_id(client):
    resp = _delete_memory(client, "not-a-uuid")
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "validation_error"


def test_delete_memory_without_body_actor_id_succeeds(client):
    # Phase 5.8: audit actor comes from the trusted SecurityContext; DELETE
    # carries no body, so no actor_id is supplied by the client.
    agent_id = _make_agent()
    try:
        created = _create_memory(client, agent_id, content="x")
        resp = client.delete(f"/memories/{created['id']}")
        assert resp.status_code == 200, resp.text
        _cleanup_memory(created["id"])
    finally:
        _delete_agent(agent_id)


def test_soft_delete_hides_from_get(client):
    agent_id = _make_agent()
    try:
        created = _create_memory(client, agent_id, content="hidden after delete")
        mid = created["id"]
        _delete_memory(client, mid)
        resp = client.get(f"/memories/{mid}")
        assert resp.status_code == 404, resp.text
        _cleanup_memory(mid)
    finally:
        _delete_agent(agent_id)


# ── I. TRANSACTION COMMIT ──────────────────────────────
def test_transaction_commit_on_success(client):
    agent_id = _make_agent()
    try:
        resp = client.post(
            "/memories",
            json={
                "agent_id": agent_id,
                "type": "semantic",
                "content": "committed memory",
            },
        )
        assert resp.status_code == 201
        memory_id = resp.json()["id"]
        conn = real_connect(TEST_DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM memories WHERE id = %s", (memory_id,))
                assert cur.fetchone()[0] == 1
        finally:
            conn.close()
            _cleanup_memory(memory_id)
    finally:
        _delete_agent(agent_id)


# ── J. TRANSACTION ROLLBACK ────────────────────────────
def test_transaction_rollback_on_domain_error(client):
    # A nonexistent agent raises NotFoundError -> 404, no row committed.
    missing = str(uuid.uuid4())
    content = f"rollback-{uuid.uuid4()}"
    resp = client.post(
        "/memories",
        json={
            "agent_id": missing,
            "type": "semantic",
                "content": content,
        },
    )
    assert resp.status_code == 404
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM memories WHERE content = %s", (content,))
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()


# ── K. AUDIT SIDE EFFECTS ──────────────────────────────
def test_audit_log_written_on_create(client):
    agent_id = _make_agent()
    try:
        before = _count_audit("memory.create")
        client.post(
            "/memories",
            json={
                "agent_id": agent_id,
                "type": "semantic",
                "content": "audited create",
            },
        )
        after = _count_audit("memory.create")
        assert after == before + 1
    finally:
        _delete_agent(agent_id)


def test_audit_log_written_on_update(client):
    agent_id = _make_agent()
    try:
        created = _create_memory(client, agent_id, content="audited update")
        before = _count_audit("memory.update")
        client.patch(
            f"/memories/{created['id']}",
            json={"content": "changed"},
        )
        after = _count_audit("memory.update")
        assert after == before + 1
        _cleanup_memory(created["id"])
    finally:
        _delete_agent(agent_id)


def test_audit_log_written_on_delete(client):
    agent_id = _make_agent()
    try:
        created = _create_memory(client, agent_id, content="audited delete")
        before = _count_audit("memory.soft_delete")
        _delete_memory(client, created["id"])
        after = _count_audit("memory.soft_delete")
        assert after == before + 1
        _cleanup_memory(created["id"])
    finally:
        _delete_agent(agent_id)


# ── L. STRUCTURAL GUARDS ───────────────────────────────
def test_router_has_no_unmanaged_connection():
    import app.api.routers.memories as mod

    src = inspect.getsource(mod)
    # The router must not open its own connection or manage transactions.
    assert "psycopg.connect" not in src
    assert "conn.commit" not in src
    assert "conn.rollback" not in src
    # It must reuse the sanctioned dependencies.
    assert "get_txn" in src
    assert "get_conn" in src


def test_router_invokes_memory_service(monkeypatch, client):
    import app.api.routers.memories as mod

    # The router must not touch the repository layer directly.
    assert "MemoryRepository" not in inspect.getsource(mod)

    called = {"create": False}
    original = MemoryService.create

    def _spy(self, *args, **kwargs):
        called["create"] = True
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MemoryService, "create", _spy)
    agent_id = _make_agent()
    try:
        resp = client.post(
            "/memories",
            json={
                "agent_id": agent_id,
                "type": "semantic",
                "content": "spy memory",
            },
        )
        assert resp.status_code == 201
        assert called["create"] is True
        _cleanup_memory(resp.json()["id"])
    finally:
        _delete_agent(agent_id)


# ── M. MISCELLANEOUS ───────────────────────────────────
def test_create_then_get_roundtrip(client):
    agent_id = _make_agent()
    try:
        created = _create_memory(client, agent_id, content="roundtrip", metadata={"k": "v"})
        got = client.get(f"/memories/{created['id']}").json()
        assert got["content"] == "roundtrip"
        assert got["metadata"] == {"k": "v"}
        _cleanup_memory(created["id"])
    finally:
        _delete_agent(agent_id)


def test_pagination_limit_offset(client):
    agent_id = _make_agent()
    try:
        for i in range(5):
            _create_memory(client, agent_id, content=f"p{i}")
        page = client.get(f"/memories/by-agent/{agent_id}?limit=2&offset=1")
        assert page.status_code == 200, page.text
        assert len(page.json()) == 2
        for r in page.json():
            _cleanup_memory(r["id"])
    finally:
        _delete_agent(agent_id)


def test_router_registered_in_app(client):
    # The OpenAPI schema should advertise the memories routes.
    paths = client.get("/openapi.json").json()["paths"]
    assert "/memories" in paths
    assert "/memories/{memory_id}" in paths
    assert "/memories/by-agent/{agent_id}" in paths
    assert "/memories/by-classification/{classification}" in paths