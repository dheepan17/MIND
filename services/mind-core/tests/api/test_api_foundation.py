"""Focused tests for the mind-core HTTP foundation (Phase 5.1).

Covers only the boundary established in this phase:

* application imports and factory,
* ``/health`` contract (no DB required),
* FastAPI TestClient works,
* service-error -> HTTP mapping,
* DB dependency lifecycle (connection opened/closed, never unmanaged),
* transaction commit on success and rollback on domain failure.

No domain endpoints are exercised here (they do not exist yet).

Live transaction tests target the isolated ``mind_test_db`` via
``TEST_DATABASE_URL`` (the project's sanctioned test database) and clean up
after themselves, so they never touch the application ``mind_db``.
"""
from __future__ import annotations

import os

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app.api.dependencies import get_conn, get_txn
from app.db import connect
from app.main import app, create_app
from app.services.agent_service import AgentService
from app.services.errors import (
    AuthorizationError,
    NotFoundError,
    PolicyViolationError,
    ValidationError,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


# ── 1. application imports / factory ─────────────────────────
def test_app_imports():
    assert app is not None
    assert app.title == "M.I.N.D."


def test_create_app_is_factory():
    fresh = create_app()
    assert isinstance(fresh, type(app))
    # A new instance, not the module-level singleton.
    assert fresh is not app


# ── 2. /health contract ──────────────────────────────────────
def test_health_endpoint_contract():
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["system"] == "M.I.N.D."
    assert body["call_name"] == "WEB"
    assert body["status"] == "online"


def test_root_endpoint_contract():
    with TestClient(app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["status"] == "online"


# ── 3. TestClient works ──────────────────────────────────────
def test_testclient_context_manager():
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


# ── 4. service-error -> HTTP mapping ─────────────────────────
@pytest.mark.parametrize(
    "exc,status,code",
    [
        (ValidationError("owner is required"), 400, "validation_error"),
        (NotFoundError("agent missing"), 404, "not_found"),
        (AuthorizationError("no capability"), 403, "forbidden"),
        (PolicyViolationError("destructive blocked"), 403, "policy_violation"),
    ],
)
def test_service_error_mapping(exc, status, code):
    # Mount a throwaway route that raises the domain error so the registered
    # exception handlers are exercised through the full stack.
    probe = create_app()

    @probe.get("/__probe")
    def _probe():
        raise exc

    with TestClient(probe) as client:
        resp = client.get("/__probe")
    assert resp.status_code == status
    body = resp.json()
    assert body["error"] == code
    # Domain message is surfaced; no SQL/stack trace.
    assert body["detail"]
    assert "psycopg" not in body["detail"]
    assert "traceback" not in body["detail"]


def test_unexpected_exception_not_swallowed():
    # An unexpected (non-domain) error must NOT be mapped to a domain code;
    # FastAPI's default handler returns 500 and the error remains distinct.
    probe = create_app()

    @probe.get("/__boom")
    def _boom():
        raise RuntimeError("unexpected internal fault")

    # raise_server_exceptions=False so the client returns the 500 response
    # instead of re-raising the server-side RuntimeError. Starlette's default
    # 500 handler returns plain text, not JSON, so we assert on status/text.
    with TestClient(probe, raise_server_exceptions=False) as client:
        resp = client.get("/__boom")
    assert resp.status_code == 500
    # No fabricated domain error code leaked into the body.
    assert "validation_error" not in resp.text
    assert "policy_violation" not in resp.text


# ── 5. DB dependency lifecycle ───────────────────────────────
def test_get_conn_opens_and_closes(monkeypatch):
    opened = []
    real_connect = connect

    def tracking_connect(url=None):
        conn = real_connect(url)
        opened.append(conn)
        return conn

    monkeypatch.setattr("app.api.dependencies.connect", tracking_connect)

    seen = {}
    probe = create_app()

    @probe.get("/__db")
    def _db(conn=Depends(get_conn)):
        seen["conn"] = conn
        return {"ok": True}

    with TestClient(probe) as client:
        resp = client.get("/__db")
    assert resp.status_code == 200
    assert seen["conn"] is not None
    # Connection was opened by the dependency and closed on teardown.
    assert opened and opened[0] is seen["conn"]
    assert seen["conn"].closed


def test_no_unmanaged_connection_in_route():
    # Routes must use the injected dependency, which reuses the single
    # sanctioned entry point (app.db.connect). The module must not expose a
    # raw psycopg.connect call.
    import app.api.dependencies as deps

    assert deps.connect is connect


# ── 6. transaction ownership: commit on success ──────────────
def test_transaction_commits_on_success(monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set; skipping live transaction test")
    # Route's connection must target the isolated test DB, not mind_db.
    monkeypatch.setattr(
        "app.api.dependencies.connect",
        lambda url=None: connect(TEST_DATABASE_URL),
    )

    created_id = {}
    probe = create_app()

    @probe.post("/__agents")
    def _create(conn=Depends(get_txn)):
        svc = AgentService(conn)
        agent = svc.create(
            owner="api-test", purpose="tx-commit", actor_type="agent", actor_id="sys"
        )
        created_id["id"] = agent["id"]
        return {"id": str(agent["id"])}

    with TestClient(probe) as client:
        resp = client.post("/__agents")
    assert resp.status_code == 200

    # Verify the insert was committed (not rolled back) in the test DB.
    conn = connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM agents WHERE id = %s", (created_id["id"],)
            )
            assert cur.fetchone()[0] == 1
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM agents WHERE id = %s", (created_id["id"],))
        conn.commit()
        conn.close()


# ── 7. transaction ownership: rollback on domain failure ─────
def test_transaction_rolls_back_on_domain_error(monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set; skipping live transaction test")
    monkeypatch.setattr(
        "app.api.dependencies.connect",
        lambda url=None: connect(TEST_DATABASE_URL),
    )

    probe = create_app()

    @probe.post("/__agents_bad")
    def _create_bad(conn=Depends(get_txn)):
        svc = AgentService(conn)
        # Invalid autonomy_level raises ValidationError -> must roll back.
        svc.create(
            owner="api-test",
            purpose="tx-rollback",
            autonomy_level=99,
            actor_type="agent",
            actor_id="sys",
        )
        return {"ok": True}

    with TestClient(probe) as client:
        resp = client.post("/__agents_bad")
    # ValidationError maps to 400.
    assert resp.status_code == 400

    # No agent row should have been committed for this request.
    conn = connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM agents WHERE owner = 'api-test' "
                "AND purpose = 'tx-rollback'"
            )
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()