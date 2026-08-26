"""Phase 4.1 tests for the service-layer base class.

These tests are intentionally lightweight and require no PostgreSQL. They
verify that ``BaseService`` accepts an existing connection, never creates
one, and exposes repository construction bound to that same connection.
"""
from __future__ import annotations

from app.repositories.base import BaseRepository
from app.services import BaseService


class _FakeConnection:
    """A stand-in for a psycopg.Connection that records usage.

    It does not connect to anything; it only lets us assert that
    ``BaseService`` stores the connection it is given and never opens a
    new one.
    """

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:  # pragma: no cover - not exercised here
        self.closed = True


class _DummyRepo(BaseRepository):
    """Minimal repository subclass used to verify binding."""


def test_base_service_accepts_existing_connection():
    conn = _FakeConnection()
    svc = BaseService(conn)
    # The injected connection is stored as-is; no new object is created.
    assert svc.conn is conn


def test_base_service_does_not_create_connections():
    conn = _FakeConnection()
    svc = BaseService(conn)
    # Constructing a service must not open, replace, or close the conn.
    assert svc.conn is conn
    assert conn.closed is False


def test_base_service_repository_helper_binds_same_connection():
    conn = _FakeConnection()
    svc = BaseService(conn)
    repo = svc.repository(_DummyRepo)
    # The repository must operate on the exact same connection instance.
    assert isinstance(repo, _DummyRepo)
    assert repo._conn is conn


def test_base_service_repository_helper_is_repeatable():
    conn = _FakeConnection()
    svc = BaseService(conn)
    first = svc.repository(_DummyRepo)
    second = svc.repository(_DummyRepo)
    # Each call returns a fresh repository, but both share the connection.
    assert first is not second
    assert first._conn is second._conn is conn


def test_base_service_is_usable_via_subclass():
    class _Concrete(BaseService):
        def echo_conn(self):
            return self.conn

    conn = _FakeConnection()
    svc = _Concrete(conn)
    assert svc.echo_conn() is conn