"""Dependency-injection helpers for the mind-core API (Phase 5.1).

Reuses the existing connection and transaction primitives. Routes receive a
connection that is already wrapped in an explicit transaction: the request's
work is committed on success and rolled back if a service/domain error
propagates. No route creates its own connection or commits/rolls back.

The connection is always obtained through :func:`app.db.connection.connect`
so application code can never open an unmanaged ``psycopg.connect`` and can
never touch the isolated test database (``TEST_DATABASE_URL`` is ignored by
``connect``).
"""
from __future__ import annotations

from collections.abc import Iterator

import psycopg

from app.db import connect, transaction


def get_conn() -> Iterator[psycopg.Connection]:
    """Yield a connection from the application URL; close it on teardown."""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def get_txn() -> Iterator[psycopg.Connection]:
    """Yield a connection owned by an explicit request transaction.

    The connection is opened via :func:`connect` (never an unmanaged
    ``psycopg.connect``) and wrapped in :class:`transaction` so the request's
    database work commits on success and rolls back if a domain error escapes.
    The connection is always closed afterwards.
    """
    conn = connect()
    try:
        with transaction(conn):
            yield conn
    finally:
        conn.close()