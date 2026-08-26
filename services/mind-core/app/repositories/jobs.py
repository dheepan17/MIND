"""Job persistence repository (Phase 3).

Pure persistence for the ``jobs`` table (async execution records). No retry
policy, no scheduling policy, no error logging.
"""
from __future__ import annotations

from app.repositories.base import BaseRepository


class JobRepository(BaseRepository):
    def create(
        self,
        task_id: str | None = None,
        agent_id: str | None = None,
        tool_id: str | None = None,
        status: str = "queued",
        scheduled_at: object | None = None,
    ) -> dict:
        sql = (
            "INSERT INTO jobs (task_id, agent_id, tool_id, status, "
            "scheduled_at) VALUES (%s, %s, %s, %s, %s) RETURNING *"
        )
        with self._cur() as cur:
            cur.execute(
                sql, (task_id, agent_id, tool_id, status, scheduled_at)
            )
            return cur.fetchone()

    def get(self, job_id: str) -> dict | None:
        sql = "SELECT * FROM jobs WHERE id = %s"
        with self._cur() as cur:
            cur.execute(sql, (job_id,))
            return cur.fetchone()

    def mark_started(self, job_id: str) -> None:
        sql = (
            "UPDATE jobs SET status = 'running', started_at = now() "
            "WHERE id = %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (job_id,))

    def mark_finished(self, job_id: str, result: object | None) -> None:
        sql = (
            "UPDATE jobs SET status = 'succeeded', finished_at = now(), "
            "result = %s WHERE id = %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (self._jsonb(result), job_id))

    def mark_failed(self, job_id: str, error: str | None) -> None:
        # Errors are persisted, never logged here.
        sql = (
            "UPDATE jobs SET status = 'failed', finished_at = now(), "
            "error = %s WHERE id = %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (error, job_id))

    def retry(self, job_id: str) -> None:
        """Re-queue a failed job and increment retry_count atomically."""
        sql = (
            "UPDATE jobs "
            "SET status = 'queued', "
            "retry_count = retry_count + 1, "
            "finished_at = NULL, "
            "error = NULL "
            "WHERE id = %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (job_id,))
    def increment_retry(self, job_id: str) -> None:
        sql = "UPDATE jobs SET retry_count = retry_count + 1 WHERE id = %s"
        with self._cur() as cur:
           cur.execute(sql, (job_id,))
    def list_by_task(
        self, task_id: str, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        # jobs has no created_at; order by the scheduling timestamp. The
        # id tiebreaker makes pagination a deterministic total order even
        # when scheduled_at is NULL (the default) or shared within a
        # transaction.
        sql = (
            "SELECT * FROM jobs WHERE task_id = %s "
            "ORDER BY scheduled_at DESC, id DESC LIMIT %s OFFSET %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (task_id, limit, offset))
            return cur.fetchall()
        
    def list_by_agent(
        self, agent_id: str, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        sql = (
            "SELECT * FROM jobs WHERE agent_id = %s "
            "ORDER BY scheduled_at DESC, id DESC LIMIT %s OFFSET %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (agent_id, limit, offset))
            return cur.fetchall()
        
    def list_by_status(
        self, status: str, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        # jobs has no created_at; order by the scheduling timestamp. The
        # id tiebreaker makes pagination a deterministic total order even
        # when scheduled_at is NULL (the default) or shared within a
        # transaction.
        sql = (
            "SELECT * FROM jobs WHERE status = %s "
            "ORDER BY scheduled_at DESC, id DESC LIMIT %s OFFSET %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (status, limit, offset))
            return cur.fetchall()
