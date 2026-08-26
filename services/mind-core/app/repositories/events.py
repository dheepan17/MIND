"""Event persistence repository (Phase 3).

Append-only persistence for the ``events`` table. No update/delete methods,
no cryptographic verification, no event business logic.
"""
from __future__ import annotations

from app.repositories.base import BaseRepository


class EventRepository(BaseRepository):
    def append(
        self,
        type: str,
        producer_agent_id: str | None,
        correlation_id: str | None,
        payload: object,
        signature: str | None = None,
        is_security_event: bool = False,
    ) -> dict:
        sql = (
            "INSERT INTO events (type, producer_agent_id, correlation_id, "
            "payload, signature, is_security_event) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *"
        )
        with self._cur() as cur:
            cur.execute(
                sql,
                (
                    type,
                    producer_agent_id,
                    correlation_id,
                    self._jsonb(payload),
                    signature,
                    is_security_event,
                ),
            )
            return cur.fetchone()

    def get(self, event_id: str) -> dict | None:
        sql = "SELECT * FROM events WHERE id = %s"
        with self._cur() as cur:
            cur.execute(sql, (event_id,))
            return cur.fetchone()

    def list_by_correlation(
        self, correlation_id: str, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        # created_at is the transaction start time (now()), so rows inserted
        # in one transaction share an identical created_at. The id tiebreaker
        # makes pagination a deterministic total order.
        sql = (
            "SELECT * FROM events WHERE correlation_id = %s "
            "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (correlation_id, limit, offset))
            return cur.fetchall()

    def list_by_producer(
        self, producer_agent_id: str, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        # created_at is the transaction start time (now()), so rows inserted
        # in one transaction share an identical created_at. The id tiebreaker
        # makes pagination a deterministic total order.
        sql = (
            "SELECT * FROM events WHERE producer_agent_id = %s "
            "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (producer_agent_id, limit, offset))
            return cur.fetchall()

    def list_security_events(
        self, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        # created_at is the transaction start time (now()), so rows inserted
        # in one transaction share an identical created_at. The id tiebreaker
        # makes pagination a deterministic total order.
        sql = (
            "SELECT * FROM events WHERE is_security_event = true "
            "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (limit, offset))
            return cur.fetchall()