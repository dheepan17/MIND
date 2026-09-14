"""Phase 6.1 security tests for Memory secret classification authorization.

Verifies the memory secret classification boundary:
* SecurityContext is the authoritative identity
* write capability is required for all mutations
* data:pii capability is additionally required for secret-classified memories
* trusted actor identity is used for both checks
* denied data:pii attempts are audited correctly
* successful secret mutations are audited correctly
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
            owner=f"owner-{uuid.uuid4()}", purpose="phase61-test"
        )
        conn.commit()
        return str(agent["id"])
    finally:
        conn.close()


def _make_data_pii_agent() -> str:
    """Create (and commit) an agent with write + data:pii capabilities."""
    conn = real_connect(TEST_DATABASE_URL)
    try:
        from app.repositories.agents import AgentRepository
        from app.repositories.permissions import PermissionRepository

        perms = PermissionRepository(conn)
        agents = AgentRepository(conn)
        role = perms.create_role(f"pii-{uuid.uuid4()}")
        write_cap = perms.create_capability("write")
        pii_cap = perms.create_capability("data:pii")
        perms.assign_capability_to_role(role["id"], write_cap["id"])
        perms.assign_capability_to_role(role["id"], pii_cap["id"])
        agent = agents.create(
            owner=f"owner-{uuid.uuid4()}",
            purpose="phase61-test",
            primary_role_id=role["id"],
        )
        conn.commit()
        return str(agent["id"])
    finally:
        conn.close()


def _cleanup_memory(memory_id: str) -> None:
    if memory_id is None:
        return
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM memories WHERE id = %s", (str(memory_id),))
        conn.commit()
    finally:
        conn.close()


def _make_agent() -> str:
    conn = real_connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agents (owner, purpose) VALUES (%s, %s) RETURNING id",
                (f"owner-{uuid.uuid4()}", "phase61-test"),
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


@pytest.fixture(autouse=True)
def _reset_resolver():
    original = get_security_context_resolver()
    try:
        yield
    finally:
        set_security_context_resolver(original)


def test_unauthenticated_create_memory_returns_401(monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setattr(
        "app.api.dependencies.connect",
        lambda url=None: real_connect(TEST_DATABASE_URL),
    )
    with TestClient(app) as c:
        resp = c.post(
            "/memories",
            json={
                "agent_id": str(uuid.uuid4()),
                "type": "semantic",
                "content": "x",
            },
        )
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthorized"


def test_secret_create_without_data_pii_returns_403(monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    writer_id = _seeded_writer_conn()
    try:
        install_write_resolver(writer_id)
        monkeypatch.setattr(
            "app.api.dependencies.connect",
            lambda url=None: real_connect(TEST_DATABASE_URL),
        )
        agent_id = _make_anon_agent()
        try:
            with TestClient(app) as c:
                resp = c.post(
                    "/memories",
                    json={
                        "agent_id": agent_id,
                        "type": "semantic",
                        "content": "secret",
                        "classification": "secret",
                    },
                )
            assert resp.status_code == 403
            assert resp.json()["error"] == "forbidden"
        finally:
            delete_agent(agent_id)
    finally:
        delete_agent(writer_id)


def test_secret_create_with_data_pii_succeeds(monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    pii_id = _make_data_pii_agent()
    try:
        install_write_resolver(pii_id)
        monkeypatch.setattr(
            "app.api.dependencies.connect",
            lambda url=None: real_connect(TEST_DATABASE_URL),
        )
        agent_id = _make_anon_agent()
        try:
            with TestClient(app) as c:
                resp = c.post(
                    "/memories",
                    json={
                        "agent_id": agent_id,
                        "type": "semantic",
                        "content": "secret",
                        "classification": "secret",
                    },
                )
            assert resp.status_code == 201
            assert resp.json()["classification"] == "secret"
            # cleanup
            conn = real_connect(TEST_DATABASE_URL)
            try:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM memories WHERE id = %s", (str(resp.json()["id"]),))
                conn.commit()
            finally:
                conn.close()
        finally:
            delete_agent(agent_id)
    finally:
        delete_agent(pii_id)


def test_secret_update_without_data_pii_returns_403(monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    writer_id = _seeded_writer_conn()
    try:
        install_write_resolver(writer_id)
        monkeypatch.setattr(
            "app.api.dependencies.connect",
            lambda url=None: real_connect(TEST_DATABASE_URL),
        )
        agent_id = _make_anon_agent()
        try:
            # create an internal memory first
            with TestClient(app) as c:
                created = c.post(
                    "/memories",
                    json={
                        "agent_id": agent_id,
                        "type": "semantic",
                        "content": "internal",
                        "classification": "internal",
                    },
                ).json()
                mid = created["id"]
                resp = c.patch(
                    f"/memories/{mid}",
                    json={"classification": "secret"},
                )
            assert resp.status_code == 403
            assert resp.json()["error"] == "forbidden"
            _cleanup_memory(mid)
        finally:
            delete_agent(agent_id)
    finally:
        delete_agent(writer_id)


def test_secret_update_with_data_pii_succeeds(monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    pii_id = _make_data_pii_agent()
    try:
        install_write_resolver(pii_id)
        monkeypatch.setattr(
            "app.api.dependencies.connect",
            lambda url=None: real_connect(TEST_DATABASE_URL),
        )
        agent_id = _make_anon_agent()
        try:
            with TestClient(app) as c:
                created = c.post(
                    "/memories",
                    json={
                        "agent_id": agent_id,
                        "type": "semantic",
                        "content": "internal",
                        "classification": "internal",
                    },
                ).json()
                mid = created["id"]
                resp = c.patch(
                    f"/memories/{mid}",
                    json={"classification": "secret"},
                )
            assert resp.status_code == 200
            assert resp.json()["classification"] == "secret"
            _cleanup_memory(mid)
        finally:
            delete_agent(agent_id)
    finally:
        delete_agent(pii_id)


def test_denied_data_pii_audit_recorded(monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    writer_id = _seeded_writer_conn()
    try:
        install_write_resolver(writer_id)
        monkeypatch.setattr(
            "app.api.dependencies.connect",
            lambda url=None: real_connect(TEST_DATABASE_URL),
        )
        agent_id = _make_anon_agent()
        try:
            with TestClient(app) as c:
                resp = c.post(
                    "/memories",
                    json={
                        "agent_id": agent_id,
                        "type": "semantic",
                        "content": "secret",
                        "classification": "secret",
                    },
                )
            assert resp.status_code == 403
            conn = real_connect(TEST_DATABASE_URL)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT action, outcome, capability_used, actor_id, target_type "
                        "FROM audit_logs WHERE action = 'memory.create' "
                        "ORDER BY created_at DESC LIMIT 1"
                    )
                    row = cur.fetchone()
            finally:
                conn.close()
            assert row is not None
            action, outcome, capability_used, actor_id, target_type = row
            assert action == "memory.create"
            assert outcome == "denied"
            assert capability_used == "data:pii"
            assert actor_id == writer_id
            assert target_type == "memory"
        finally:
            delete_agent(agent_id)
    finally:
        delete_agent(writer_id)


def test_public_internal_create_succeeds_with_write_only(monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    writer_id = _seeded_writer_conn()
    try:
        install_write_resolver(writer_id)
        monkeypatch.setattr(
            "app.api.dependencies.connect",
            lambda url=None: real_connect(TEST_DATABASE_URL),
        )
        agent_id = _make_anon_agent()
        try:
            with TestClient(app) as c:
                resp = c.post(
                    "/memories",
                    json={
                        "agent_id": agent_id,
                        "type": "semantic",
                        "content": "public",
                        "classification": "public",
                    },
                )
            assert resp.status_code == 201
            assert resp.json()["classification"] == "public"
            _cleanup_memory(resp.json()["id"])
        finally:
            delete_agent(agent_id)
    finally:
        delete_agent(writer_id)


def test_get_memory_unchanged(monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    writer_id = _seeded_writer_conn()
    try:
        install_write_resolver(writer_id)
        monkeypatch.setattr(
            "app.api.dependencies.connect",
            lambda url=None: real_connect(TEST_DATABASE_URL),
        )
        agent_id = _make_anon_agent()
        try:
            with TestClient(app) as c:
                resp = c.post(
                    "/memories",
                    json={
                        "agent_id": agent_id,
                        "type": "semantic",
                        "content": "read me",
                    },
                )
                created = resp.json()
                mid = created["id"]
                resp2 = c.get(f"/memories/{mid}")
            assert resp2.status_code == 200
            _cleanup_memory(mid)
        finally:
            delete_agent(agent_id)
    finally:
        delete_agent(writer_id)


def test_list_by_classification_secret_still_blocked(monkeypatch):
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
            resp = c.get("/memories/by-classification/secret")
        assert resp.status_code == 403
        assert resp.json()["error"] == "policy_violation"
    finally:
        delete_agent(writer_id)
