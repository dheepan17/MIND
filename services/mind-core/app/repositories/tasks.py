"""Task persistence repository (Phase 3).

Pure persistence for the ``tasks`` table, including the self-referential
hierarchy. No business rules, no scheduling logic.
"""
from __future__ import annotations

from app.repositories.base import BaseRepository


class TaskRepository(BaseRepository):
    def create(
        self,
        agent_id: str | None,
        title: str,
        status: str = "pending",
        priority: int = 5,
        parent_task_id: str | None = None,
        description: str | None = None,
        input_payload: object | None = None,
        metadata: object | None = None,
    ) -> dict:
        sql = (
            "INSERT INTO tasks (agent_id, title, status, priority, "
            "parent_task_id, description, input_payload, metadata) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *"
        )
        with self._cur() as cur:
            cur.execute(
                sql,
                (
                    agent_id,
                    title,
                    status,
                    priority,
                    parent_task_id,
                    description,
                    self._jsonb(input_payload),
                    self._jsonb(metadata),
                ),
            )
            return cur.fetchone()

    def get(self, task_id: str) -> dict | None:
        sql = "SELECT * FROM tasks WHERE id = %s"
        with self._cur() as cur:
            cur.execute(sql, (task_id,))
            return cur.fetchone()

    def update_status(self, task_id: str, status: str) -> None:
        # completed_at reflects the completion contract; set on completion.
        if status == "completed":
            sql = (
                "UPDATE tasks SET status = %s, completed_at = now(), "
                "updated_at = now() WHERE id = %s"
            )
        else:
            sql = (
                "UPDATE tasks SET status = %s, updated_at = now() "
                "WHERE id = %s"
            )
        with self._cur() as cur:
            cur.execute(sql, (status, task_id))

    def assign_agent(self, task_id: str, agent_id: str | None) -> None:
        sql = (
            "UPDATE tasks SET agent_id = %s, updated_at = now() "
            "WHERE id = %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (agent_id, task_id))

    def list_children(
        self, parent_task_id: str, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        # created_at is the transaction start time (now()), so rows inserted
        # in one transaction share an identical created_at. The id tiebreaker
        # makes pagination a deterministic total order.
        sql = (
            "SELECT * FROM tasks WHERE parent_task_id = %s "
            "ORDER BY created_at ASC, id ASC LIMIT %s OFFSET %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (parent_task_id, limit, offset))
            return cur.fetchall()

    def list_by_agent(
        self, agent_id: str, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        sql = (
            "SELECT * FROM tasks WHERE agent_id = %s "
            "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (agent_id, limit, offset))
            return cur.fetchall()

    def list_by_status(
        self, status: str, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        sql = (
            "SELECT * FROM tasks WHERE status = %s "
            "ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (status, limit, offset))
            return cur.fetchall()

    def list_subtree(self, root_task_id: str, limit: int = 100) -> list[dict]:
        # Static recursive CTE; no user-provided SQL fragments.
        sql = (
            "WITH RECURSIVE subtree AS ("
            "  SELECT * FROM tasks WHERE id = %s "
            "  UNION ALL "
            "  SELECT t.* FROM tasks t "
            "  INNER JOIN subtree s ON t.parent_task_id = s.id"
            ") SELECT * FROM subtree LIMIT %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (root_task_id, limit))
            return cur.fetchall()