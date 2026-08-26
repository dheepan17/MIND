"""Tool persistence repository (Phase 3).

Pure persistence for the ``tools`` table. No safety decisions, no capability
authorization, no config logging.
"""
from __future__ import annotations

from app.repositories.base import BaseRepository


class ToolRepository(BaseRepository):
    def create(
        self,
        name: str,
        description: str | None = None,
        version: str | None = None,
        requires_capability: str | None = None,
        is_destructive: bool = False,
        is_verified: bool = False,
        config: object | None = None,
        status: str = "active",
    ) -> dict:
        sql = (
            "INSERT INTO tools (name, description, version, "
            "requires_capability, is_destructive, is_verified, config, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *"
        )
        with self._cur() as cur:
            cur.execute(
                sql,
                (
                    name,
                    description,
                    version,
                    requires_capability,
                    is_destructive,
                    is_verified,
                    self._jsonb(config),
                    status,
                ),
            )
            return cur.fetchone()

    def get(self, tool_id: str) -> dict | None:
        sql = "SELECT * FROM tools WHERE id = %s"
        with self._cur() as cur:
            cur.execute(sql, (tool_id,))
            return cur.fetchone()

    def get_by_name(self, name: str) -> dict | None:
        sql = "SELECT * FROM tools WHERE name = %s"
        with self._cur() as cur:
            cur.execute(sql, (name,))
            return cur.fetchone()

    def update_status(self, tool_id: str, status: str) -> None:
        sql = "UPDATE tools SET status = %s WHERE id = %s"
        with self._cur() as cur:
            cur.execute(sql, (status, tool_id))

    def mark_verified(self, tool_id: str, is_verified: bool = True) -> None:
        # Verification state is stored only; enforcement is elsewhere.
        sql = "UPDATE tools SET is_verified = %s WHERE id = %s"
        with self._cur() as cur:
            cur.execute(sql, (is_verified, tool_id))

    def list_destructive(
        self, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        sql = (
            "SELECT * FROM tools WHERE is_destructive = true "
            "ORDER BY name ASC LIMIT %s OFFSET %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (limit, offset))
            return cur.fetchall()

    def list_by_capability(
        self, capability_key: str, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        sql = (
            "SELECT * FROM tools WHERE requires_capability = %s "
            "ORDER BY name ASC LIMIT %s OFFSET %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (capability_key, limit, offset))
            return cur.fetchall()