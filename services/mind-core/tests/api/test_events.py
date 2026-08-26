"""Phase 5.5 tests for the Event HTTP API.

Exercises the thin transport boundary over :class:`EventService` through the
real FastAPI app and :class:`fastapi.testclient.TestClient`. Every test that
touches the database targets the isolated ``mind_test_db`` via
``TEST_DATABASE_URL`` (the project's sanctioned test database) by redirecting
the API's connection dependency; tests skip cleanly when that URL is unset.

The router must remain a transport adapter: it calls ``EventService`` and
nothing else (no SQL, no repository, no connection management, no commit/
rollback, no audit mirroring). Those invariants are asserted directly.
"""
from __future__ import annotations

import inspect
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import connect as real_connect
from app.api.routers import events as events_router
from app.main import app
from app.services.event_service import EventService

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
def client(monkeypatch):
    """TestClient whose DB dependency targets the isolated test database."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set; skipping live Event API tests")
    # Redirect the single sanctioned connection entry point used by the API
    # dependencies to the isolated test DB (never mind_db).
    monkeypatch.setattr(
        "app.api.dependencies.connect",
        lambda url=None: real_connect(TEST_DATABASE_URL),
    )
    with TestClient(app) as c:
        yield c


def _cleanup_event(event_id: str) -> None:
    """Remove a committed event (test hygiene only)."""
    if event_id is None:
        return
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM events WHERE id = %s", (str(event_id),))
        conn.commit()
    finally:
        conn.close()


def _make_agent() -> str:
    """Create a throwaway agent directly for event setup."""
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agents (owner, purpose) VALUES (%s, %s) "
                "RETURNING id",
                (f"owner-{uuid.uuid4()}", "event-api-setup"),
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


def _post_event(client, **overrides) -> dict:
    payload = {
        "type": f"evt-{uuid.uuid4()}",
        "payload": {"k": "v"},
    }
    payload.update(overrides)
    return client.post("/events", json=payload).json()


# ── A. ROUTE REGISTRATION ───────────────────────────────
def test_all_routes_registered():
    # Inspect the router directly (app.routes wraps it in an _IncludedRouter).
    paths = {route.path for route in events_router.router.routes}
    assert "/events" in paths
    assert "/events/{event_id}" in paths
    assert "/events/by-correlation/{correlation_id}" in paths
    assert "/events/by-producer/{producer_agent_id}" in paths
    assert "/events/security" in paths


def test_security_not_swallowed_by_event_id():
    # /events/security must resolve to the security list route, not /{event_id}.
    for route in events_router.router.routes:
        if route.path == "/events/security":
            assert "GET" in route.methods
            return
    pytest.fail("/events/security route not registered")


def test_only_post_and_get_exposed():
    # Append-only: no PATCH/PUT/DELETE on the events router.
    methods = set()
    for route in events_router.router.routes:
        if route.path.startswith("/events"):
            methods |= route.methods
    assert "POST" in methods
    assert "GET" in methods
    assert "PATCH" not in methods
    assert "PUT" not in methods
    assert "DELETE" not in methods


# ── B. CREATE SUCCESS ───────────────────────────────────
def test_post_event_success(client):
    agent_id = _make_agent()
    try:
        resp = client.post(
            "/events",
            json={
                "type": f"evt-{uuid.uuid4()}",
                "payload": {"hello": "world"},
                "producer_agent_id": agent_id,
                "is_security_event": False,
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["type"]
        assert body["payload"] == {"hello": "world"}
        assert body["producer_agent_id"] == agent_id
        assert body["is_security_event"] is False
        assert body["id"]
        assert body["created_at"]
        _cleanup_event(body["id"])
    finally:
        _delete_agent(agent_id)

def test_post_event_with_signature_transported_verbatim(client):
    agent_id = _make_agent()
    try:
        sig = "deadbeef-signature"
        resp = client.post(
            "/events",
            json={
                "type": f"evt-{uuid.uuid4()}",
                "payload": {"x": 1},
                "producer_agent_id": agent_id,
                "signature": sig,
            },
        )
        assert resp.status_code == 201
        # Signature is stored as supplied; no verification occurs here.
        assert resp.json()["signature"] == sig
        _cleanup_event(resp.json()["id"])
    finally:
        _delete_agent(agent_id)


# ── C. CREATE VALIDATION ────────────────────────────────
def test_post_event_missing_type(client):
    # Missing `type` -> Pydantic transport validation -> 422.
    resp = client.post("/events", json={"payload": {"a": 1}})
    assert resp.status_code == 422


def test_post_event_missing_payload(client):
    resp = client.post("/events", json={"type": "evt"})
    assert resp.status_code == 422


def test_post_event_empty_type(client):
    # Empty type -> service ValidationError -> 400.
    resp = client.post("/events", json={"type": "", "payload": {"a": 1}})
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


def test_post_event_null_payload(client):
    # Null payload -> service ValidationError -> 400.
    resp = client.post("/events", json={"type": "evt", "payload": None})
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


def test_post_event_malformed_producer_uuid(client):
    resp = client.post(
        "/events",
        json={
            "type": "evt",
            "payload": {"a": 1},
            "producer_agent_id": "not-a-uuid",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


def test_post_event_malformed_correlation_uuid(client):
    resp = client.post(
        "/events",
        json={
            "type": "evt",
            "payload": {"a": 1},
            "correlation_id": "not-a-uuid",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


def test_post_event_invalid_signature_type(client):
    # signature must be a string or None.
    resp = client.post(
        "/events",
        json={"type": "evt", "payload": {"a": 1}, "signature": 123},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


def test_post_event_invalid_is_security_event(client):
    resp = client.post(
        "/events",
        json={"type": "evt", "payload": {"a": 1}, "is_security_event": "yes"},
    )
    assert resp.status_code == 422


# ── D. PRODUCER VALIDATION ──────────────────────────────
def test_post_event_nonexistent_producer(client):
    missing = str(uuid.uuid4())
    resp = client.post(
        "/events",
        json={
            "type": "evt",
            "payload": {"a": 1},
            "producer_agent_id": missing,
        },
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


# ── E. GET BY ID ────────────────────────────────────────
def test_get_event_success(client):
    agent_id = _make_agent()
    try:
        created = _post_event(client, producer_agent_id=agent_id)
        resp = client.get(f"/events/{created['id']}")
        assert resp.status_code == 200
        assert resp.json()["id"] == created["id"]
        _cleanup_event(created["id"])
    finally:
        _delete_agent(agent_id)

def test_get_event_not_found(client):
    missing = str(uuid.uuid4())
    resp = client.get(f"/events/{missing}")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


def test_get_event_malformed_uuid(client):
    resp = client.get("/events/not-a-uuid")
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


# ── F. GET BY CORRELATION ───────────────────────────────
def test_get_by_correlation(client):
    agent_id = _make_agent()
    corr = str(uuid.uuid4())
    try:
        for _ in range(2):
            _post_event(
                client, producer_agent_id=agent_id, correlation_id=corr
            )
        resp = client.get(f"/events/by-correlation/{corr}")
        assert resp.status_code == 200
        assert len(resp.json()) == 2
        assert all(r["correlation_id"] == corr for r in resp.json())
        for r in resp.json():
            _cleanup_event(r["id"])
    finally:
        _delete_agent(agent_id)


def test_get_by_correlation_invalid_uuid(client):
    resp = client.get("/events/by-correlation/not-a-uuid")
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


# ── G. GET BY PRODUCER ──────────────────────────────────
def test_get_by_producer(client):
    agent_id = _make_agent()
    try:
        for _ in range(2):
            _post_event(client, producer_agent_id=agent_id)
        resp = client.get(f"/events/by-producer/{agent_id}")
        assert resp.status_code == 200
        assert len(resp.json()) == 2
        assert all(r["producer_agent_id"] == agent_id for r in resp.json())
        for r in resp.json():
            _cleanup_event(r["id"])
    finally:
        _delete_agent(agent_id)


def test_get_by_producer_invalid_uuid(client):
    resp = client.get("/events/by-producer/not-a-uuid")
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


# ── H. SECURITY EVENTS ─────────────────────────────────
def test_get_security_events(client):
    agent_id = _make_agent()
    try:
        sec = _post_event(
            client, producer_agent_id=agent_id, is_security_event=True
        )
        _post_event(client, producer_agent_id=agent_id, is_security_event=False)
        resp = client.get("/events/security")
        assert resp.status_code == 200
        rows = resp.json()
        assert all(r["is_security_event"] is True for r in rows)
        assert any(r["id"] == sec["id"] for r in rows)
        for r in rows:
            _cleanup_event(r["id"])
    finally:
        _delete_agent(agent_id)


# ── I. TRANSACTION COMMIT ──────────────────────────────
def test_transaction_commit_on_success(client):
    agent_id = _make_agent()
    try:
        resp = client.post(
            "/events",
            json={
                "type": f"evt-{uuid.uuid4()}",
                "payload": {"a": 1},
                "producer_agent_id": agent_id,
            },
        )
        assert resp.status_code == 201
        event_id = resp.json()["id"]
        conn = real_connect(TEST_DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM events WHERE id = %s", (event_id,)
                )
                assert cur.fetchone()[0] == 1
        finally:
            conn.close()
            _cleanup_event(event_id)
    finally:
        _delete_agent(agent_id)

# ── J. TRANSACTION ROLLBACK ────────────────────────────
def test_transaction_rollback_on_domain_error(client):
    # A nonexistent producer raises NotFoundError -> 404, no row committed.
    missing = str(uuid.uuid4())
    type_ = f"rollback-{uuid.uuid4()}"
    resp = client.post(
        "/events",
        json={
            "type": type_,
            "payload": {"a": 1},
            "producer_agent_id": missing,
        },
    )
    assert resp.status_code == 404
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM events WHERE type = %s", (type_,)
            )
            assert cur.fetchone()[0] == 0
    finally:
        conn.close()


# ── K. STRUCTURAL GUARDS ───────────────────────────────
def test_router_has_no_unmanaged_connection():
    import app.api.routers.events as mod

    src = inspect.getsource(mod)
    # The router must not open its own connection or manage transactions.
    assert "psycopg.connect" not in src
    assert "conn.commit" not in src
    assert "conn.rollback" not in src
    # It must reuse the sanctioned dependencies.
    assert "get_txn" in src
    assert "get_conn" in src


def test_router_invokes_event_service(monkeypatch, client):
    import app.api.routers.events as mod

    # The router must not touch the repository layer directly.
    assert "EventRepository" not in inspect.getsource(mod)

    called = {"create": False}
    original = EventService.create

    def _spy(self, *args, **kwargs):
        called["create"] = True
        return original(self, *args, **kwargs)

    monkeypatch.setattr(EventService, "create", _spy)
    agent_id = _make_agent()
    try:
        resp = client.post(
            "/events",
            json={
                "type": f"evt-{uuid.uuid4()}",
                "payload": {"a": 1},
                "producer_agent_id": agent_id,
            },
        )
        assert resp.status_code == 201
        assert called["create"] is True
        _cleanup_event(resp.json()["id"])
    finally:
        _delete_agent(agent_id)


# ── L. EVENT-SPECIFIC INVARIANTS ───────────────────────
def test_no_audit_record_generated_by_router(client):
    agent_id = _make_agent()
    try:
        resp = client.post(
            "/events",
            json={
                "type": f"evt-{uuid.uuid4()}",
                "payload": {"a": 1},
                "producer_agent_id": agent_id,
            },
        )
        assert resp.status_code == 201
        event_id = resp.json()["id"]
        conn = real_connect(TEST_DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM audit_logs WHERE target_id = %s",
                    (str(event_id),),
                )
                # Event creation must NOT mirror into audit_logs.
                assert cur.fetchone()[0] == 0
        finally:
            conn.close()
            _cleanup_event(event_id)
    finally:
        _delete_agent(agent_id)

def test_events_remain_append_only(client):
    # No mutation/delete endpoints exist on the events router.
    for route in events_router.router.routes:
        if route.path.startswith("/events"):
            assert route.methods <= {"GET", "POST"}
