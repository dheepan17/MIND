"""Phase 5.3 tests for the Task HTTP API.

Exercises the thin transport boundary over :class:`TaskService` through the
real FastAPI app and :class:`fastapi.testclient.TestClient`. Every test that
touches the database targets the isolated ``mind_test_db`` via
``TEST_DATABASE_URL`` (the project's sanctioned test database) by redirecting
the API's connection dependency; tests skip cleanly when that URL is unset.

The router must remain a transport adapter: it calls ``TaskService`` and
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
from app.services.task_service import TaskService

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def client(monkeypatch):
    """Authenticated TestClient (write-capable principal) over the test DB."""
    from tests.security.helpers import auth_client

    app, _agent_id = auth_client(monkeypatch)
    with TestClient(app) as c:
        yield c


def _cleanup_task(task_id: str) -> None:
    """Remove a committed task and its audit rows (test hygiene only)."""
    if task_id is None:
        return
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM audit_logs WHERE target_id = %s", (str(task_id),)
            )
            cur.execute("DELETE FROM tasks WHERE id = %s", (str(task_id),))
        conn.commit()
    finally:
        conn.close()


def _make_agent() -> str:
    """Create a throwaway agent directly for task setup."""
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agents (owner, purpose) VALUES (%s, %s) "
                "RETURNING id",
                (f"owner-{uuid.uuid4()}", "task-api-setup"),
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


def _make_task(client, agent_id: str, **overrides) -> dict:
    payload = {
        "title": f"task-{uuid.uuid4()}",
        "agent_id": agent_id,
    }
    payload.update(overrides)
    return client.post("/tasks", json=payload).json()


# ── 1. POST /tasks success ───────────────────────────────
def test_post_task_success(client):
    agent_id = _make_agent()
    try:
        resp = client.post(
            "/tasks",
            json={
                "title": f"task-{uuid.uuid4()}",
                "agent_id": agent_id,
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["title"]
        assert body["status"] == "pending"
        assert body["priority"] == 5
        assert body["agent_id"] == agent_id
        assert body["id"]
        _cleanup_task(body["id"])
    finally:
        _delete_agent(agent_id)


# ── 2. POST /tasks validation failure ────────────────────
def test_post_task_validation_failure(client):
    agent_id = _make_agent()
    try:
        # Invalid status must be rejected by the service -> 400.
        resp = client.post(
            "/tasks",
            json={
                "title": "bad status",
                "status": "bogus",
                "agent_id": agent_id,
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "validation_error"
    finally:
        _delete_agent(agent_id)


# ── 3. POST /tasks missing required field ────────────────
def test_post_task_missing_required_field(client):
    # Missing `title` -> Pydantic transport validation -> 422.
    resp = client.post("/tasks", json={})
    assert resp.status_code == 422


# ── 4. GET /tasks/{id} success ───────────────────────────
def test_get_task_success(client):
    agent_id = _make_agent()
    try:
        created = _make_task(client, agent_id)
        resp = client.get(f"/tasks/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == created["id"]
        _cleanup_task(created["id"])
    finally:
        _delete_agent(agent_id)


# ── 5. GET /tasks/{id} nonexistent → 404 ────────────────
def test_get_task_not_found(client):
    missing = str(uuid.uuid4())
    resp = client.get(f"/tasks/{missing}")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


# ── 6. GET /tasks/{id} malformed UUID → validation ──────
def test_get_task_malformed_uuid(client):
    # Path param is str; the service validates UUID shape -> 400.
    resp = client.get("/tasks/not-a-uuid")
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


# ── 7. PATCH status success ──────────────────────────────
def test_patch_status_success(client):
    agent_id = _make_agent()
    try:
        created = _make_task(client, agent_id)
        resp = client.patch(
            f"/tasks/{created['id']}/status",
            json={"status": "running"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"
        _cleanup_task(created["id"])
    finally:
        _delete_agent(agent_id)


# ── 8. PATCH status invalid value ────────────────────────
def test_patch_status_invalid(client):
    agent_id = _make_agent()
    try:
        created = _make_task(client, agent_id)
        resp = client.patch(
            f"/tasks/{created['id']}/status",
            json={"status": "nope"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "validation_error"
        _cleanup_task(created["id"])
    finally:
        _delete_agent(agent_id)


# ── 9. PATCH assign-agent success ────────────────────────
def test_patch_assign_agent_success(client):
    agent_id = _make_agent()
    other_agent = _make_agent()
    try:
        created = _make_task(client, agent_id)
        resp = client.patch(
            f"/tasks/{created['id']}/assign-agent",
            json={"agent_id": other_agent},
        )
        assert resp.status_code == 200
        assert resp.json()["agent_id"] == other_agent
        _cleanup_task(created["id"])
    finally:
        _delete_agent(agent_id)
        _delete_agent(other_agent)


# ── 10. PATCH assign-agent unassign ──────────────────────
def test_patch_assign_agent_unassign(client):
    agent_id = _make_agent()
    try:
        created = _make_task(client, agent_id)
        resp = client.patch(
            f"/tasks/{created['id']}/assign-agent",
            json={"agent_id": None},
        )
        assert resp.status_code == 200
        assert resp.json()["agent_id"] is None
        _cleanup_task(created["id"])
    finally:
        _delete_agent(agent_id)


# ── 11. GET /tasks/{id}/children ─────────────────────────
def test_get_children(client):
    agent_id = _make_agent()
    try:
        parent = _make_task(client, agent_id)
        for _ in range(3):
            _make_task(client, agent_id, parent_task_id=parent["id"])
        resp = client.get(f"/tasks/{parent['id']}/children")
        assert resp.status_code == 200
        assert len(resp.json()) == 3
        _cleanup_task(parent["id"])
    finally:
        _delete_agent(agent_id)


# ── 12. GET /tasks/{id}/subtree ──────────────────────────
def test_get_subtree(client):
    agent_id = _make_agent()
    try:
        root = _make_task(client, agent_id)
        child = _make_task(client, agent_id, parent_task_id=root["id"])
        _make_task(client, agent_id, parent_task_id=child["id"])
        resp = client.get(f"/tasks/{root['id']}/subtree")
        assert resp.status_code == 200
        # root + child + grandchild
        assert len(resp.json()) == 3
        _cleanup_task(root["id"])
    finally:
        _delete_agent(agent_id)

# ── 13. GET /tasks/by-agent/{agent_id} ───────────────────
def test_get_by_agent(client):
    agent_id = _make_agent()
    try:
        for _ in range(2):
            _make_task(client, agent_id)
        resp = client.get(f"/tasks/by-agent/{agent_id}")
        assert resp.status_code == 200
        assert len(resp.json()) == 2
        assert all(r["agent_id"] == agent_id for r in resp.json())
        _cleanup_task(resp.json()[0]["id"])
        _cleanup_task(resp.json()[1]["id"])
    finally:
        _delete_agent(agent_id)

# ── 14. GET /tasks/by-status/{status} ────────────────────
def test_get_by_status(client):
    agent_id = _make_agent()
    try:
        for _ in range(2):
            _make_task(client, agent_id, status="running")
        resp = client.get("/tasks/by-status/running")
        assert resp.status_code == 200
        assert len(resp.json()) == 2
        assert all(r["status"] == "running" for r in resp.json())
        for r in resp.json():
            _cleanup_task(r["id"])
    finally:
        _delete_agent(agent_id)


# ── 15. GET /tasks/by-status invalid ─────────────────────
def test_get_by_status_invalid(client):
    resp = client.get("/tasks/by-status/bogus")
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


# ── 16. transaction commit on successful mutation ────────
def test_transaction_commit_on_success(client):
    agent_id = _make_agent()
    try:
        resp = client.post(
            "/tasks",
            json={
                "title": f"task-{uuid.uuid4()}",
                "agent_id": agent_id,
            },
        )
        assert resp.status_code == 201
        task_id = resp.json()["id"]
        conn = real_connect(TEST_DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM tasks WHERE id = %s", (task_id,))
                assert cur.fetchone()[0] == 1
        finally:
            conn.close()
            _cleanup_task(task_id)
    finally:
        _delete_agent(agent_id)

# ── 17. transaction rollback when domain error occurs ────
def test_transaction_rollback_on_domain_error(client):
    agent_id = _make_agent()
    try:
        title = f"rollback-{uuid.uuid4()}"
        resp = client.post(
            "/tasks",
            json={
                "title": title,
                "status": "bogus",
                "agent_id": agent_id,
            },
        )
        assert resp.status_code == 400
        conn = real_connect(TEST_DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM tasks WHERE title = %s", (title,)
                )
                assert cur.fetchone()[0] == 0
        finally:
            conn.close()
    finally:
        _delete_agent(agent_id)


# ── 18. no unmanaged connection in router ────────────────
def test_router_has_no_unmanaged_connection():
    import app.api.routers.tasks as mod

    src = inspect.getsource(mod)
    # The router must not open its own connection or manage transactions.
    assert "psycopg.connect" not in src
    assert "conn.commit" not in src
    assert "conn.rollback" not in src
    # It must reuse the sanctioned dependencies.
    assert "get_txn" in src
    assert "get_conn" in src


# ── 19. router does not bypass TaskService ───────────────
def test_router_invokes_task_service(monkeypatch, client):
    import app.api.routers.tasks as mod

    # The router must not touch the repository layer directly.
    assert "TaskRepository" not in inspect.getsource(mod)

    called = {"create": False}
    original = TaskService.create

    def _spy(self, *args, **kwargs):
        called["create"] = True
        return original(self, *args, **kwargs)

    monkeypatch.setattr(TaskService, "create", _spy)
    agent_id = _make_agent()
    try:
        resp = client.post(
            "/tasks",
            json={
                "title": f"task-{uuid.uuid4()}",
                "agent_id": agent_id,
            },
        )
        assert resp.status_code == 201
        assert called["create"] is True
        _cleanup_task(resp.json()["id"])
    finally:
        _delete_agent(agent_id)


# ── 20. audit record exists after successful mutation ────
def test_audit_record_after_success(client):
    agent_id = _make_agent()
    actor = f"actor-{uuid.uuid4()}"
    try:
        resp = client.post(
            "/tasks",
            json={
                "title": f"task-{uuid.uuid4()}",
                "agent_id": agent_id,
            },
        )
        assert resp.status_code == 201
        task_id = resp.json()["id"]
        conn = real_connect(TEST_DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT action, outcome, actor_id FROM audit_logs "
                    "WHERE target_type = 'task' AND target_id = %s",
                    (task_id,),
                )
                rows = cur.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "task.create"
            assert rows[0][1] == "success"
            assert rows[0][2] is not None
        finally:
            conn.close()
            _cleanup_task(task_id)
    finally:
        _delete_agent(agent_id)