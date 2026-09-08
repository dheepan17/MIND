"""Phase 5.4 tests for the Job HTTP API.

Exercises the thin transport boundary over :class:`JobService` through the
real FastAPI app and :class:`fastapi.testclient.TestClient`. Every test that
touches the database targets the isolated ``mind_test_db`` via
``TEST_DATABASE_URL`` (the project's sanctioned test database) by redirecting
the API's connection dependency; tests skip cleanly when that URL is unset.

The router must remain a transport adapter: it calls ``JobService`` and
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
from app.services.job_service import JobService

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def client(monkeypatch):
    """Authenticated TestClient (write-capable principal) over the test DB."""
    from tests.security.helpers import auth_client

    app, _agent_id = auth_client(monkeypatch)
    with TestClient(app) as c:
        yield c


def _cleanup_job(job_id: str) -> None:
    """Remove a committed job and its audit rows (test hygiene only)."""
    if job_id is None:
        return
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM audit_logs WHERE target_id = %s", (str(job_id),)
            )
            cur.execute("DELETE FROM jobs WHERE id = %s", (str(job_id),))
        conn.commit()
    finally:
        conn.close()


def _make_agent() -> str:
    """Create a throwaway agent directly for job setup."""
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agents (owner, purpose) VALUES (%s, %s) "
                "RETURNING id",
                (f"owner-{uuid.uuid4()}", "job-api-setup"),
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


def _make_task(agent_id: str) -> str:
    """Create a throwaway task directly for job linkage."""
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tasks (title, agent_id) VALUES (%s, %s) "
                "RETURNING id",
                (f"task-{uuid.uuid4()}", agent_id),
            )
            tid = cur.fetchone()[0]
        conn.commit()
        return str(tid)
    finally:
        conn.close()


def _delete_task(task_id: str) -> None:
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
        conn.commit()
    finally:
        conn.close()


def _make_job(client, agent_id: str, **overrides) -> dict:
    payload = {

        "agent_id": agent_id,
    }
    payload.update(overrides)
    return client.post("/jobs", json=payload).json()


# ── 1. POST /jobs success ───────────────────────────────
def test_post_job_success(client):
    agent_id = _make_agent()
    try:
        resp = client.post(
            "/jobs",
            json={

                "agent_id": agent_id,
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "queued"
        assert body["retry_count"] == 0
        assert body["agent_id"] == agent_id
        assert body["id"]
        _cleanup_job(body["id"])
    finally:
        _delete_agent(agent_id)

# ── 2. POST /jobs validation failure ────────────────────
def test_post_job_validation_failure(client):
    agent_id = _make_agent()
    try:
        # Invalid status must be rejected by the service -> 400.
        resp = client.post(
            "/jobs",
            json={
                "status": "bogus",
                "agent_id": agent_id,
            },
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "validation_error"
    finally:
        _delete_agent(agent_id)


# ── 3. POST /jobs missing required field ────────────────
def test_post_job_missing_required_field(client):
    # The audit actor now comes from the trusted SecurityContext, so a job
    # only needs its own domain fields. Missing `agent_id` ->
    # Pydantic transport validation -> 422.
    resp = client.post("/jobs", json={})
    assert resp.status_code == 422


# ── 4. GET /jobs/{id} success ───────────────────────────
def test_get_job_success(client):
    agent_id = _make_agent()
    try:
        created = _make_job(client, agent_id)
        resp = client.get(f"/jobs/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == created["id"]
        _cleanup_job(created["id"])
    finally:
        _delete_agent(agent_id)


# ── 5. GET /jobs/{id} nonexistent → 404 ────────────────
def test_get_job_not_found(client):
    missing = str(uuid.uuid4())
    resp = client.get(f"/jobs/{missing}")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


# ── 6. GET /jobs/{id} malformed UUID → validation ──────
def test_get_job_malformed_uuid(client):
    # Path param is str; the service validates UUID shape -> 400.
    resp = client.get("/jobs/not-a-uuid")
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


# ── 7. POST /jobs/{id}/start success ───────────────────
def test_start_job_success(client):
    agent_id = _make_agent()
    try:
        created = _make_job(client, agent_id)
        resp = client.post(
            f"/jobs/{created['id']}/start",
            json={},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"
        assert resp.json()["started_at"] is not None
        _cleanup_job(created["id"])
    finally:
        _delete_agent(agent_id)


# ── 8. POST /jobs/{id}/finish success ──────────────────
def test_finish_job_success(client):
    agent_id = _make_agent()
    try:
        created = _make_job(client, agent_id)
        client.post(
            f"/jobs/{created['id']}/start",
            json={},
        )
        resp = client.post(
            f"/jobs/{created['id']}/finish",
            json={"result": {"ok": True}},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "succeeded"
        assert resp.json()["finished_at"] is not None
        assert resp.json()["result"] == {"ok": True}
        _cleanup_job(created["id"])
    finally:
        _delete_agent(agent_id)

# ── 9. POST /jobs/{id}/fail success ────────────────────
def test_fail_job_success(client):
    agent_id = _make_agent()
    try:
        created = _make_job(client, agent_id)
        resp = client.post(
            f"/jobs/{created['id']}/fail",
            json={"error": "boom"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "failed"
        assert resp.json()["error"] == "boom"
        _cleanup_job(created["id"])
    finally:
        _delete_agent(agent_id)


# ── 10. POST /jobs/{id}/retry success ───────────────────
def test_retry_job_success(client):
    agent_id = _make_agent()
    try:
        created = _make_job(client, agent_id)
        client.post(
            f"/jobs/{created['id']}/fail",
            json={"error": "boom"},
        )
        resp = client.post(
            f"/jobs/{created['id']}/retry",
            json={},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "queued"
        assert resp.json()["retry_count"] == 1
        _cleanup_job(created["id"])
    finally:
        _delete_agent(agent_id)

# ── 11. GET /jobs/by-task/{task_id} ─────────────────────
def test_get_by_task(client):
    agent_id = _make_agent()
    task_id = _make_task(agent_id)
    try:
        for _ in range(2):
            _make_job(client, agent_id, task_id=task_id)
        resp = client.get(f"/jobs/by-task/{task_id}")
        assert resp.status_code == 200
        assert len(resp.json()) == 2
        assert all(r["task_id"] == task_id for r in resp.json())
        for r in resp.json():
            _cleanup_job(r["id"])
    finally:
        _delete_task(task_id)
        _delete_agent(agent_id)

# ── 12. GET /jobs/by-agent/{agent_id} ───────────────────
def test_get_by_agent(client):
    agent_id = _make_agent()
    try:
        for _ in range(2):
            _make_job(client, agent_id)
        resp = client.get(f"/jobs/by-agent/{agent_id}")
        assert resp.status_code == 200
        assert len(resp.json()) == 2
        assert all(r["agent_id"] == agent_id for r in resp.json())
        for r in resp.json():
            _cleanup_job(r["id"])
    finally:
        _delete_agent(agent_id)


# ── 13. GET /jobs/by-status/{status} ────────────────────
def test_get_by_status(client):
    agent_id = _make_agent()
    try:
        for _ in range(2):
            _make_job(client, agent_id, status="running")
        resp = client.get("/jobs/by-status/running")
        assert resp.status_code == 200
        assert len(resp.json()) == 2
        assert all(r["status"] == "running" for r in resp.json())
        for r in resp.json():
            _cleanup_job(r["id"])
    finally:
        _delete_agent(agent_id)

# ── 14. GET /jobs/by-status invalid ─────────────────────
def test_get_by_status_invalid(client):
    resp = client.get("/jobs/by-status/bogus")
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


# ── 15. transaction commit on successful mutation ────────
def test_transaction_commit_on_success(client):
    agent_id = _make_agent()
    try:
        resp = client.post(
            "/jobs",
            json={

                "agent_id": agent_id,
            },
        )
        assert resp.status_code == 201
        job_id = resp.json()["id"]
        conn = real_connect(TEST_DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM jobs WHERE id = %s", (job_id,))
                assert cur.fetchone()[0] == 1
        finally:
            conn.close()
            _cleanup_job(job_id)
    finally:
        _delete_agent(agent_id)

# ── 16. transaction rollback when domain error occurs ────
def test_transaction_rollback_on_domain_error(client):
    agent_id = _make_agent()
    try:
        resp = client.post(
            "/jobs",
            json={
                "status": "bogus",
                "agent_id": agent_id,
            },
        )
        assert resp.status_code == 400
        conn = real_connect(TEST_DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM jobs WHERE agent_id = %s", (agent_id,)
                )
                assert cur.fetchone()[0] == 0
        finally:
            conn.close()
    finally:
        _delete_agent(agent_id)

# ── 17. no unmanaged connection in router ────────────────
def test_router_has_no_unmanaged_connection():
    import app.api.routers.jobs as mod

    src = inspect.getsource(mod)
    # The router must not open its own connection or manage transactions.
    assert "psycopg.connect" not in src
    assert "conn.commit" not in src
    assert "conn.rollback" not in src
    # It must reuse the sanctioned dependencies.
    assert "get_txn" in src
    assert "get_conn" in src


# ── 18. router does not bypass JobService ───────────────
def test_router_invokes_job_service(monkeypatch, client):
    import app.api.routers.jobs as mod

    # The router must not touch the repository layer directly.
    assert "JobRepository" not in inspect.getsource(mod)

    called = {"create": False}
    original = JobService.create

    def _spy(self, *args, **kwargs):
        called["create"] = True
        return original(self, *args, **kwargs)

    monkeypatch.setattr(JobService, "create", _spy)
    agent_id = _make_agent()
    try:
        resp = client.post(
            "/jobs",
            json={

                "agent_id": agent_id,
            },
        )
        assert resp.status_code == 201
        assert called["create"] is True
        _cleanup_job(resp.json()["id"])
    finally:
        _delete_agent(agent_id)


# ── 19. audit record exists after successful mutation ────
def test_audit_record_after_success(client):
    agent_id = _make_agent()
    actor = f"actor-{uuid.uuid4()}"
    try:
        resp = client.post(
            "/jobs",
            json={"agent_id": agent_id,
            },
        )
        assert resp.status_code == 201
        job_id = resp.json()["id"]
        conn = real_connect(TEST_DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT action, outcome, actor_id FROM audit_logs "
                    "WHERE target_type = 'job' AND target_id = %s",
                    (job_id,),
                )
                rows = cur.fetchall()
            assert len(rows) == 1
            assert rows[0][0] == "job.create"
            assert rows[0][1] == "success"
            assert rows[0][2] is not None
        finally:
            conn.close()
            _cleanup_job(job_id)
    finally:
        _delete_agent(agent_id)
