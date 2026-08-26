"""Minimal transaction context manager for mind-core.

Wraps psycopg's native transaction behavior: commits when the block
succeeds and rolls back when the block raises. Exceptions are never
swallowed. No custom savepoints or pooling are implemented yet.
"""
from __future__ import annotations

import psycopg


class transaction:
    """Context manager that commits on success and rolls back on error.

    Usage::

        with transaction(conn) as cur:
            cur.execute("INSERT INTO ...")

    The connection must not be in autocommit mode (``connect()`` returns
    connections in non-autocommit mode by default).
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def __enter__(self) -> psycopg.Cursor:
        return self._conn.cursor()

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        # Never suppress exceptions.
