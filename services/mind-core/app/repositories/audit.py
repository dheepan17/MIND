"""Audit persistence repository (Phase 3).

Append-only persistence for the ``audit_logs`` table. INSERT and SELECT only;
no update or delete methods exist, so existing records cannot be modified.
"""
from __future__ import annotations

from app.repositories.base import BaseRepository


class AuditRepository(BaseRepository):
    def append(
        self,
        actor_type: str,
        actor_id: str,
        action: str,
        outcome: str,
        target_type: str | None = None,
        target_id: str | None = None,
        capability_used: str | None = None,
        context: object | None = None,
    ) -> dict:
        sql = (
            "INSERT INTO audit_logs "
            "(actor_type, actor_id, action, target_type, target_id, "
            "capability_used, outcome, context) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *"
        )
        with self._cur() as cur:
            cur.execute(
                sql,
                (
                    actor_type,
                    actor_id,
                    action,
                    target_type,
                    target_id,
                    capability_used,
                    outcome,
                    self._jsonb(context),
                ),
            )
            return cur.fetchone()

    def get(self, audit_id: int) -> dict | None:
        sql = "SELECT * FROM audit_logs WHERE id = %s"
        with self._cur() as cur:
            cur.execute(sql, (audit_id,))
            return cur.fetchone()

    def list_by_actor(
        self, actor_type: str, actor_id: str, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        sql = (
            "SELECT * FROM audit_logs "
            "WHERE actor_type = %s AND actor_id = %s "
            "ORDER BY created_at DESC LIMIT %s OFFSET %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (actor_type, actor_id, limit, offset))
            return cur.fetchall()

    def list_by_target(
        self,
        target_type: str,
        target_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        sql = (
            "SELECT * FROM audit_logs "
            "WHERE target_type = %s AND target_id = %s "
            "ORDER BY created_at DESC LIMIT %s OFFSET %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (target_type, target_id, limit, offset))
            return cur.fetchall()

    def list_by_outcome(
        self, outcome: str, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        sql = (
            "SELECT * FROM audit_logs WHERE outcome = %s "
            "ORDER BY created_at DESC LIMIT %s OFFSET %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (outcome, limit, offset))
            return cur.fetchall()