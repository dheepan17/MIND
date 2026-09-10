"""Phase 6.0 security tests for authenticated event publishing.

Verifies the event publication boundary:
* SecurityContext is the authoritative identity
* write capability is required
* producer_agent_id comes from trusted actor, not request body
* denied publication is audited with trusted identity
* successful publication is audited with trusted identity
"""
from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import connect as real_connect
from app.api.security import SecurityContext, get_security_context, get_security_context_resolver, set_security_context_resolver
from app.main import app
from tests.security.helpers import delete_agent, install_write_resolver, _seeded_writer_conn

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def _make_anon_agent() -> str:
    """Create (and commit) an agent that holds NO capabilities."""
    conn = real_connect(TEST_DATABASE_URL)
    try:
        from app.repositories.agents import AgentRepository

        agent = AgentRepository(conn).create(
            owner=f"owner-{uuid.uuid4()}", purpose="phase60-test"
        )
        conn.commit()
        return str(agent["id"])
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _reset_resolver():
    original = get_security_context_resolver()
    try:
        yield
    finally:
        set_security_context_resolver(original)


def test_unauthenticated_event_publish_returns_401(monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setattr(
        "app.api.dependencies.connect",
        lambda url=None: real_connect(TEST_DATABASE_URL),
    )
    with TestClient(app) as c:
        resp = c.post(
            "/events",
            json={"type": "evt", "payload": {}},
        )
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


def test_authenticated_without_write_returns_403(monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    anon_id = _make_anon_agent()
    try:
        install_write_resolver(anon_id)
        monkeypatch.setattr(
            "app.api.dependencies.connect",
            lambda url=None: real_connect(TEST_DATABASE_URL),
        )
        with TestClient(app) as c:
            resp = c.post(
                "/events",
                json={"type": "evt", "payload": {}},
            )
        assert resp.status_code == 403
        assert resp.json()["error"] == "forbidden"
    finally:
        delete_agent(anon_id)


def test_authenticated_with_write_succeeds(monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    writer_id = _seeded_writer_conn()
    try:
        install_write_resolver(writer_id)
        monkeypatch.setattr(
            "app.api.dependencies.connect",
            lambda url=None: real_connect(TEST_DATABASE_URL),
        )
        with TestClient(app) as c:
            resp = c.post(
                "/events",
                json={"type": "evt", "payload": {"k": "v"}},
            )
        assert resp.status_code == 201
        body = resp.json()
        assert body["producer_agent_id"] == writer_id
        # cleanup
        conn = real_connect(TEST_DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM events WHERE id = %s", (str(body["id"]),))
            conn.commit()
        finally:
            conn.close()
    finally:
        delete_agent(writer_id)


def test_forged_body_producer_is_ignored(monkeypatch):
    """Client sends producer_agent_id=B; stored producer must be trusted A."""
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    anon_id = _make_anon_agent()
    writer_id = _seeded_writer_conn()
    try:
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
                        "type": "evt",
                        "payload": {"k": "v"},
                        "producer_agent_id": forged_id,
                    },
                )
            assert resp.status_code == 201
            body = resp.json()
            assert body["producer_agent_id"] == writer_id
            assert body["producer_agent_id"] != forged_id
            # cleanup
            conn = real_connect(TEST_DATABASE_URL)
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM events WHERE id = %s", (str(body["id"]),))
                conn.commit()
            finally:
                conn.close()
        finally:
            set_security_context_resolver(_writer_resolver)
    finally:
        delete_agent(writer_id)
        delete_agent(anon_id)


def test_denied_publication_is_audited(monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    anon_id = _make_anon_agent()
    try:
        install_write_resolver(anon_id)
        monkeypatch.setattr(
            "app.api.dependencies.connect",
            lambda url=None: real_connect(TEST_DATABASE_URL),
        )
        with TestClient(app) as c:
            resp = c.post(
                "/events",
                json={"type": "evt", "payload": {}},
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
        assert actor_id == anon_id
        assert target_type == "event"
    finally:
        delete_agent(anon_id)


def test_successful_publication_is_audited(monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    writer_id = _seeded_writer_conn()
    try:
        install_write_resolver(writer_id)
        monkeypatch.setattr(
            "app.api.dependencies.connect",
            lambda url=None: real_connect(TEST_DATABASE_URL),
        )
        with TestClient(app) as c:
            resp = c.post(
                "/events",
                json={"type": "evt", "payload": {"k": "v"}},
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
        # cleanup
        conn = real_connect(TEST_DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM events WHERE id = %s", (str(event_id),))
            conn.commit()
        finally:
            conn.close()
    finally:
        delete_agent(writer_id)


def test_signature_does_not_bypass_auth(monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    anon_id = _make_anon_agent()
    try:
        install_write_resolver(anon_id)
        monkeypatch.setattr(
            "app.api.dependencies.connect",
            lambda url=None: real_connect(TEST_DATABASE_URL),
        )
        with TestClient(app) as c:
            resp = c.post(
                "/events",
                json={
                    "type": "evt",
                    "payload": {"x": 1},
                    "signature": "fake-sig",
                },
            )
        assert resp.status_code == 403
    finally:
        delete_agent(anon_id)


def test_signature_is_persisted_verbatim(monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    writer_id = _seeded_writer_conn()
    try:
        install_write_resolver(writer_id)
        monkeypatch.setattr(
            "app.api.dependencies.connect",
            lambda url=None: real_connect(TEST_DATABASE_URL),
        )
        sig = "deadbeef-signature"
        with TestClient(app) as c:
            resp = c.post(
                "/events",
                json={
                    "type": "evt",
                    "payload": {"x": 1},
                    "signature": sig,
                },
            )
        assert resp.status_code == 201
        assert resp.json()["signature"] == sig
        # cleanup
        conn = real_connect(TEST_DATABASE_URL)
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM events WHERE id = %s", (str(resp.json()["id"]),))
            conn.commit()
        finally:
            conn.close()
    finally:
        delete_agent(writer_id)
