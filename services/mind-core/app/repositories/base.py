"""Base repository for mind-core persistence (Phase 3).

Repositories are thin, explicit-SQL persistence adapters. They:

* accept an existing ``psycopg.Connection`` (never create one),
* never commit, rollback, or close the connection,
* never expose arbitrary SQL execution to callers,
* propagate database errors (never swallow them).

Transaction ownership belongs to the service / caller layer.
"""
from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb


class BaseRepository:
    """Stores an existing connection and provides dict-row cursors.

    Subclasses implement persistence operations using explicit, fully
    parameterized SQL. No ORM, no query builder, no generic ``execute_sql``.
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def _cur(self) -> psycopg.Cursor:
        """Return a cursor that yields column->value dicts."""
        return self._conn.cursor(row_factory=psycopg.rows.dict_row)

    @staticmethod
    def _jsonb(value):
        """Adapt a Python object to PostgreSQL JSONB, or pass SQL NULL.

        ``None`` is passed through as a SQL NULL rather than a JSON ``null``
        so nullable JSONB columns retain proper NULL semantics.
        """
        return Jsonb(value) if value is not None else None
