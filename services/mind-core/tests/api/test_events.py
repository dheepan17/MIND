"""Phase 5.5 + Phase 6.0 tests for the Event HTTP API.

Exercises the thin transport boundary over :class:`EventService` through the
real FastAPI app and :class:`fastapi.testclient.TestClient`. Every test that
touches the database targets the isolated ``mind_test_db`` via
``TEST_DATABASE_URL`` (the project's sanctioned test database) by redirecting
the API's connection dependency; tests skip cleanly when that URL is unset.

The router must remain a transport adapter: it calls ``EventService`` and
nothing else (no SQL, no repository, no connection management, no commit/
rollback). Those invariants are asserted directly.

Phase 6.0 adds authenticated publishing and producer identity binding:
POST /events requires SecurityContext + write capability, and the stored
producer_agent_id is the trusted ctx.actor_id, never the request body.
"""
from __future__ import annotations

import inspect
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import connect as real_connect
from app.api.routers import events as events_router
from app.api.security import SecurityContext, get_security_context, set_security_context_resolver
from app.main import app
from app.services.event_service import EventService
from tests.security.helpers import delete_agent, install_write_resolver, _seeded_writer_conn

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def _make_anon_agent() -> str:
    """Create (and commit) an agent that holds NO capabilities."""
    conn = real_connect(TEST_DATABASE_URL)
    try:
        from app.repositories.agents import AgentRepository

        agent = AgentRepository(conn).create(
            owner=f"owner-{uuid.uuid4()}", purpose="event-api-setup"
        )
        conn.commit()
        return str(agent["id"])
    finally:
        conn.close()


@pytest.fixture
def client(monkeypatch):
    """Authenticated TestClient (write-capable principal) over the test DB."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set; skipping live Event API tests")
    writer_id = _seeded_writer_conn()
    install_write_resolver(writer_id)
    monkeypatch.setattr(
        "app.api.dependencies.connect",
        lambda url=None: real_connect(TEST_DATABASE_URL),
    )
    with TestClient(app) as c:
        yield c, writer_id
    delete_agent(writer_id)


@pytest.fixture
def client_anon(monkeypatch):
    """Authenticated TestClient without write capability."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set; skipping live Event API tests")
    agent_id = _make_anon_agent()
    install_write_resolver(agent_id)
    with TestClient(app) as c:
        yield c
    delete_agent(agent_id)


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
    paths = {route.path for route in events_router.router.routes}
    assert "/events" in paths
    assert "/events/{event_id}" in paths
    assert "/events/by-correlation/{correlation_id}" in paths
    assert "/events/by-producer/{producer_agent_id}" in paths
    assert "/events/security" in paths


def test_security_not_swallowed_by_event_id():
    for route in events_router.router.routes:
        if route.path == "/events/security":
            assert "GET" in route.methods
            return
    pytest.fail("/events/security route not registered")


def test_only_post_and_get_exposed():
    methods = set()
    for route in events_router.router.routes:
        if route.path.startswith("/events"):
            methods |= route.methods
    assert "POST" in methods
    assert "GET" in methods
    assert "PATCH" not in methods
    assert "PUT" not in methods
    assert "DELETE" not in methods


# ── B. PHASE 6.0 AUTHENTICATION ─────────────────────────
class TestPhase60Authentication:
    def test_unauthenticated_create_event(self, monkeypatch):
        monkeypatch.setattr(
            "app.api.dependencies.connect",
            lambda url=None: real_connect(TEST_DATABASE_URL),
        )
        with TestClient(app) as c:
            resp = c.post(
                "/events",
                json={
                    "type": f"evt-{uuid.uuid4()}",
                    "payload": {"hello": "world"},
                },
            )
        assert resp.status_code == 401
        assert resp.json()["error"] == "unauthorized"

    def test_authenticated_without_write_returns_403(self, client_anon):
        resp = client_anon.post(
            "/events",
            json={
                "type": f"evt-{uuid.uuid4()}",
                "payload": {"hello": "world"},
            },
        )
        assert resp.status_code == 403
        assert resp.json()["error"] == "forbidden"

    def test_authenticated_with_write_succeeds(self, client):
        c, _ = client
        resp = c.post(
            "/events",
            json={
                "type": f"evt-{uuid.uuid4()}",
                "payload": {"hello": "world"},
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["type"]
        assert body["payload"] == {"hello": "world"}
        _cleanup_event(body["id"])


# ── C. PRODUCER IDENTITY BINDING (anti-spoofing) ─────────
class TestProducerIdentityBinding:
    def test_stored_producer_equals_trusted_actor_id(self, client):
        c, writer_id = client
        resp = c.post(
            "/events",
            json={
                "type": f"evt-{uuid.uuid4()}",
                "payload": {"k": "v"},
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["producer_agent_id"] == writer_id
        _cleanup_event(body["id"])

    def test_forged_body_producer_is_ignored(self, monkeypatch):
        """Client sends producer_agent_id=B; stored producer must be trusted A."""
        if not TEST_DATABASE_URL:
            pytest.skip("TEST_DATABASE_URL not set")
        anon_id = _make_anon_agent()
        try:
            writer_id = _seeded_writer_conn()
            def _writer_resolver(request):
                return SecurityContext(
                    actor_type="agent",
                    actor_id=writer_id,
                    authenticated=True,
                    source="external",
                )
            set_security_context_resolver(_writer_resolver)
            try:
                monkeypatch.setattr(
                    "app.api.dependencies.connect",
                    lambda url=None: real_connect(TEST_DATABASE_URL),
                )
                forged_id = str(uuid.uuid4())
                with TestClient(app) as c:
                    resp = c.post(
                        "/events",
                        json={
                            "type": f"evt-{uuid.uuid4()}",
                            "payload": {"k": "v"},
                            "producer_agent_id": forged_id,
                        },
                    )
                assert resp.status_code == 201
                body = resp.json()
                assert body["producer_agent_id"] == writer_id
                assert body["producer_agent_id"] != forged_id
                _cleanup_event(body["id"])
            finally:
                set_security_context_resolver(_writer_resolver)
            delete_agent(writer_id)
        finally:
            delete_agent(anon_id)

    def test_forged_actor_id_cannot_bypass_authorization(self, monkeypatch):
        """Body actor_id is not a field; trusted ctx.actor_id controls auth."""
        if not TEST_DATABASE_URL:
            pytest.skip("TEST_DATABASE_URL not set")
        anon_id = _make_anon_agent()
        try:
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
                    resp = c.post(
                        "/events",
                        json={
                            "type": f"evt-{uuid.uuid4()}",
                            "payload": {"k": "v"},
                        },
                    )
                assert resp.status_code == 403
            finally:
                set_security_context_resolver(_anon_resolver)
        finally:
            delete_agent(anon_id)


# ── D. SIGNATURE TRANSPORT ──────────────────────────────
class TestSignatureTransport:
    def test_signature_persisted_verbatim(self, client):
        c, _ = client
        sig = "deadbeef-signature"
        resp = c.post(
            "/events",
            json={
                "type": f"evt-{uuid.uuid4()}",
                "payload": {"x": 1},
                "signature": sig,
            },
        )
        assert resp.status_code == 201
        assert resp.json()["signature"] == sig
        _cleanup_event(resp.json()["id"])

    def test_signature_does_not_bypass_auth(self, monkeypatch):
        if not TEST_DATABASE_URL:
            pytest.skip("TEST_DATABASE_URL not set")
        anon_id = _make_anon_agent()
        try:
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
                    resp = c.post(
                        "/events",
                        json={
                            "type": f"evt-{uuid.uuid4()}",
                            "payload": {"x": 1},
                            "signature": "fake-sig",
                        },
                    )
                assert resp.status_code == 403
            finally:
                set_security_context_resolver(_anon_resolver)
        finally:
            delete_agent(anon_id)


# ── E. AUDIT ────────────────────────────────────────────
class TestAudit:
    def test_success_audit_recorded(self, client):
        c, writer_id = client
        resp = c.post(
            "/events",
            json={
                "type": f"evt-{uuid.uuid4()}",
                "payload": {"k": "v"},
            },
        )
        assert resp.status_code == 201
        event_id = resp.json()["id"]
        conn = real_connect(TEST_DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT action, outcome, capability_used, actor_id, target_id, target_type "
                    "FROM audit_logs WHERE target_id = %s ORDER BY created_at DESC LIMIT 1",
                    (str(event_id),),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None
        action, outcome, capability_used, actor_id, target_id, target_type = row
        assert action == "event.create"
        assert outcome == "success"
        assert capability_used == "write"
        assert actor_id == writer_id
        assert target_id == str(event_id)
        assert target_type == "event"
        _cleanup_event(event_id)

    def test_denied_audit_recorded(self, client_anon):
        resp = client_anon.post(
            "/events",
            json={
                "type": f"evt-{uuid.uuid4()}",
                "payload": {"k": "v"},
            },
        )
        assert resp.status_code == 403
        conn = real_connect(TEST_DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT action, outcome, capability_used, actor_id, target_type "
                    "FROM audit_logs WHERE action = 'event.create' "
                    "ORDER BY created_at DESC LIMIT 1"
                )
                row = cur.fetchone()
        finally:
            conn.close()
        assert row is not None
        action, outcome, capability_used, actor_id, target_type = row
        assert action == "event.create"
        assert outcome == "denied"
        assert capability_used == "write"
        assert actor_id == client_anon
        assert target_type == "event"


# ── F. CREATE SUCCESS ───────────────────────────────────
class TestCreateSuccess:
    def test_post_event_success(self, client):
        c, _ = client
        resp = c.post(
            "/events",
            json={
                "type": f"evt-{uuid.uuid4()}",
                "payload": {"hello": "world"},
                "is_security_event": False,
            },
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["type"]
        assert body["payload"] == {"hello": "world"}
        assert body["is_security_event"] is False
        assert body["id"]
        assert body["created_at"]
        _cleanup_event(body["id"])

    def test_post_event_with_signature_transported_verbatim(self, client):
        c, _ = client
        sig = "deadbeef-signature"
        resp = c.post(
            "/events",
            json={
                "type": f"evt-{uuid.uuid4()}",
                "payload": {"x": 1},
                "signature": sig,
            },
        )
        assert resp.status_code == 201
        assert resp.json()["signature"] == sig
        _cleanup_event(resp.json()["id"])


# ── G. CREATE VALIDATION ────────────────────────────────
def test_post_event_missing_type(client):
    c, _ = client
    resp = c.post("/events", json={"payload": {"a": 1}})
    assert resp.status_code == 422


def test_post_event_missing_payload(client):
    c, _ = client
    resp = c.post("/events", json={"type": "evt"})
    assert resp.status_code == 422


def test_post_event_empty_type(client):
    c, _ = client
    resp = c.post("/events", json={"type": "", "payload": {"a": 1}})
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


def test_post_event_null_payload(client):
    c, _ = client
    resp = c.post("/events", json={"type": "evt", "payload": None})
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


def test_post_event_malformed_correlation_uuid(client):
    c, _ = client
    resp = c.post(
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
    c, _ = client
    resp = c.post(
        "/events",
        json={"type": "evt", "payload": {"a": 1}, "signature": 123},
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


def test_post_event_invalid_is_security_event(client):
    c, _ = client
    resp = c.post(
        "/events",
        json={"type": "evt", "payload": {"a": 1}, "is_security_event": "yes"},
    )
    assert resp.status_code == 422


# ── H. GET BY ID ────────────────────────────────────────
def test_get_event_success(client):
    c, _ = client
    created = _post_event(c)
    resp = c.get(f"/events/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]
    _cleanup_event(created["id"])


def test_get_event_not_found(client):
    c, _ = client
    missing = str(uuid.uuid4())
    resp = c.get(f"/events/{missing}")
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


def test_get_event_malformed_uuid(client):
    c, _ = client
    resp = c.get("/events/not-a-uuid")
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


# ── I. GET BY CORRELATION ───────────────────────────────
def test_get_by_correlation(client):
    c, _ = client
    corr = str(uuid.uuid4())
    for _ in range(2):
        _post_event(c, correlation_id=corr)
    resp = c.get(f"/events/by-correlation/{corr}")
    assert resp.status_code == 200
    assert len(resp.json()) == 2
    assert all(r["correlation_id"] == corr for r in resp.json())
    for r in resp.json():
        _cleanup_event(r["id"])


def test_get_by_correlation_invalid_uuid(client):
    c, _ = client
    resp = c.get("/events/by-correlation/not-a-uuid")
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


# ── J. GET BY PRODUCER ──────────────────────────────────
def test_get_by_producer(client):
    c, writer_id = client
    for _ in range(2):
        _post_event(c)
    resp = c.get(f"/events/by-producer/{writer_id}")
    assert resp.status_code == 200
    assert len(resp.json()) == 2
    assert all(r["producer_agent_id"] == writer_id for r in resp.json())
    for r in resp.json():
        _cleanup_event(r["id"])


def test_get_by_producer_invalid_uuid(client):
    c, _ = client
    resp = c.get("/events/by-producer/not-a-uuid")
    assert resp.status_code == 400
    assert resp.json()["error"] == "validation_error"


# ── K. SECURITY EVENTS ─────────────────────────────────
def test_get_security_events(client):
    c, _ = client
    sec = _post_event(c, is_security_event=True)
    _post_event(c, is_security_event=False)
    resp = c.get("/events/security")
    assert resp.status_code == 200
    rows = resp.json()
    assert all(r["is_security_event"] is True for r in rows)
    assert any(r["id"] == sec["id"] for r in rows)
    for r in rows:
        _cleanup_event(r["id"])


# ── L. TRANSACTION COMMIT ──────────────────────────────
def test_transaction_commit_on_success(client):
    c, _ = client
    resp = c.post(
        "/events",
        json={
            "type": f"evt-{uuid.uuid4()}",
            "payload": {"a": 1},
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


# ── M. TRANSACTION ROLLBACK ────────────────────────────
def test_transaction_rollback_on_auth_failure(monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    anon_id = _make_anon_agent()
    try:
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
            type_ = f"rollback-{uuid.uuid4()}"
            with TestClient(app) as c:
                resp = c.post(
                    "/events",
                    json={
                        "type": type_,
                        "payload": {"a": 1},
                    },
                )
            assert resp.status_code == 403
            conn = real_connect(TEST_DATABASE_URL)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM events WHERE type = %s", (type_,)
                    )
                    assert cur.fetchone()[0] == 0
            finally:
                conn.close()
        finally:
            set_security_context_resolver(_anon_resolver)
    finally:
        delete_agent(anon_id)


# ── N. STRUCTURAL GUARDS ───────────────────────────────
def test_router_has_no_unmanaged_connection():
    src = inspect.getsource(events_router)
    assert "psycopg.connect" not in src
    assert "conn.commit" not in src
    assert "conn.rollback" not in src
    assert "get_txn" in src
    assert "get_conn" in src


def test_router_invokes_event_service(monkeypatch, client):
    import app.api.routers.events as mod

    assert "EventRepository" not in inspect.getsource(mod)

    called = {"create": False}
    original = EventService.create

    def _spy(self, *args, **kwargs):
        called["create"] = True
        return original(self, *args, **kwargs)

    monkeypatch.setattr(EventService, "create", _spy)
    c, _ = client
    resp = c.post(
        "/events",
        json={
            "type": f"evt-{uuid.uuid4()}",
            "payload": {"a": 1},
        },
    )
    assert resp.status_code == 201
    assert called["create"] is True
    _cleanup_event(resp.json()["id"])


def test_router_obtains_security_context():
    src = inspect.getsource(events_router)
    assert "get_security_context" in src
    assert "SecurityContext" in src


def test_no_direct_repository_access_in_router():
    src = inspect.getsource(events_router)
    assert "EventRepository" not in src


# ── O. EVENT-SPECIFIC INVARIANTS ───────────────────────
def test_audit_record_generated_for_event_creation(client):
    c, _ = client
    resp = c.post(
        "/events",
        json={
            "type": f"evt-{uuid.uuid4()}",
            "payload": {"a": 1},
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
            # Phase 6.0: event creation IS mirrored into audit_logs now.
            assert cur.fetchone()[0] == 1
    finally:
        conn.close()
        _cleanup_event(event_id)


def test_events_remain_append_only(client):
    c, _ = client
    for route in events_router.router.routes:
        if route.path.startswith("/events"):
            assert route.methods <= {"GET", "POST"}
