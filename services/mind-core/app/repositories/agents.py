"""Agent persistence repository (Phase 3).

Pure persistence for the ``agents`` table. No authorization, no autonomy
interpretation, no business logic.
"""
from __future__ import annotations

from app.repositories.base import BaseRepository


class AgentRepository(BaseRepository):
    def create(
        self,
        owner: str,
        purpose: str,
        autonomy_level: int = 0,
        primary_role_id: str | None = None,
        status: str = "active",
    ) -> dict:
        sql = (
            "INSERT INTO agents (owner, purpose, autonomy_level, "
            "primary_role_id, status) VALUES (%s, %s, %s, %s, %s) "
            "RETURNING *"
        )
        with self._cur() as cur:
            cur.execute(
                sql, (owner, purpose, autonomy_level, primary_role_id, status)
            )
            return cur.fetchone()

    def get(self, agent_id: str) -> dict | None:
        sql = "SELECT * FROM agents WHERE id = %s"
        with self._cur() as cur:
            cur.execute(sql, (agent_id,))
            return cur.fetchone()

    def update_status(self, agent_id: str, status: str) -> None:
        sql = (
            "UPDATE agents SET status = %s, updated_at = now() "
            "WHERE id = %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (status, agent_id))

    def update_autonomy_level(self, agent_id: str, autonomy_level: int) -> None:
        # Stored only; interpretation belongs to the authorization layer.
        sql = (
            "UPDATE agents SET autonomy_level = %s, updated_at = now() "
            "WHERE id = %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (autonomy_level, agent_id))

    def list_by_owner(
        self, owner: str, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        # created_at is the transaction start time (now()), so rows inserted
        # in one transaction share an identical created_at. The id tiebreaker
        # makes pagination a deterministic total order.
        sql = (
            "SELECT * FROM agents WHERE owner = %s "
            "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (owner, limit, offset))
            return cur.fetchall()

    def list_by_role(
        self, role_id: str, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        sql = (
            "SELECT * FROM agents WHERE primary_role_id = %s "
            "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (role_id, limit, offset))
            return cur.fetchall()