"""Base class for the mind-core service layer (Phase 4.1).

Services own business rules, authorization, validation, and transaction
orchestration. They sit above the repository layer and below any
transport (HTTP, CLI, etc.).

Design constraints (see Phase 4.1 spec):

* A service receives an **existing** ``psycopg.Connection``; it never
  creates one and never reads ``DATABASE_URL`` / ``TEST_DATABASE_URL``.
* A service never commits or rolls back directly. Transaction boundaries
  are owned by concrete service methods via the existing
  ``transaction(conn)`` context manager.
* ``BaseService`` does **not** automatically wrap every method in a
  transaction, because some operations compose multiple services inside
  a single transaction. Transaction ownership stays explicit.
* Services contain no SQL and never query PostgreSQL except through the
  repositories. No ORM, no async, no connection pooling.

Dependency injection is preferred over hidden globals: construct a
service with the connection it should use.
"""
from __future__ import annotations

import psycopg

from app.repositories.base import BaseRepository


class BaseService:
    """Stores an existing connection and provides repository helpers.

    Subclasses implement business logic. This base is intentionally
    lightweight: it holds the connection, exposes a small helper to build
    repositories bound to the same connection, and contains no business
    logic or SQL.
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        # Store the injected connection; never open a new one.
        self._conn = conn

    @property
    def conn(self) -> psycopg.Connection:
        """The injected connection this service operates on."""
        return self._conn

    def repository(self, repo_cls: type[BaseRepository]) -> BaseRepository:
        """Construct a repository bound to this service's connection.

        Keeps concrete services from repeating ``SomeRepo(self._conn)``
        and centralizes the injection point. The repository inherits the
        same transaction scope as the service.
        """
        return repo_cls(self._conn)