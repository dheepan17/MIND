"""Synchronous PostgreSQL connection layer for mind-core.

A small, explicit wrapper around psycopg 3. It intentionally avoids:

* global/persistent connections (callers open and close per unit of work),
* connection pooling (added later behind this same API if needed),
* any ORM functionality.

Connections are always obtained through :func:`connect`, which reads the
database URL from the application configuration unless an explicit URL is
provided (used by tests and internal callers).
"""
from __future__ import annotations

import psycopg

from app.config import load_config


class DatabaseError(Exception):
    """Base class for database-layer errors."""


class DatabaseConfigError(DatabaseError):
    """Raised when a required database URL is missing or unusable."""


def connect(url: str | None = None) -> psycopg.Connection:
    """Open a synchronous PostgreSQL connection.

    The URL is resolved in this order:

    1. an explicit ``url`` argument (tests / internal callers),
    2. ``DATABASE_URL`` from the application configuration.

    ``TEST_DATABASE_URL`` is *never* used here; test code must pass it
    explicitly so application code can never accidentally touch the test
    database.

    Args:
        url: Optional explicit connection URL. When omitted, the
            application ``DATABASE_URL`` is used.

    Returns:
        A ``psycopg.Connection`` in non-autocommit mode. Callers are
        responsible for closing it (prefer a ``with`` block).

    Raises:
        DatabaseConfigError: if no URL is available.
    """
    resolved = url or load_config().database_url
    if not resolved:
        raise DatabaseConfigError(
            "DATABASE_URL is not set. Provide it via the environment "
            "or pass an explicit url to connect()."
        )
    return psycopg.connect(resolved, autocommit=False)