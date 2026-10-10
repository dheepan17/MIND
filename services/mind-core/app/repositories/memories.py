"""Memory persistence repository (Phase 3).

Pure persistence for the ``memories`` table, including soft deletion and
classification. No authorization, no embedding similarity search, no pgvector.
Memory content and embeddings are never logged.
"""
from __future__ import annotations

from app.repositories.base import BaseRepository

_UNSET = object()


class MemoryRepository(BaseRepository):
    @staticmethod
    def _normalize(row: dict | None) -> dict | None:
        """Normalize a memory row for the public API.

        ``embedding`` is stored as ``NUMERIC[]``; PostgreSQL returns its
        elements as ``Decimal``. The repository API promises ``list[float]``,
        so convert at the read boundary without touching the schema.
        """
        if row is None:
            return None
        emb = row.get("embedding")
        if emb is not None:
            row["embedding"] = [float(v) for v in emb]
        return row

    def create(
        self,
        agent_id: str | None,
        type: str,
        classification: str = "internal",
        content: str | None = None,
        embedding: list[float] | None = None,
        metadata: object | None = None,
        retention_until: object | None = None,
    ) -> dict:
        sql = (
            "INSERT INTO memories (agent_id, type, classification, content, "
            "embedding, metadata, retention_until) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *"
        )
        with self._cur() as cur:
            cur.execute(
                sql,
                (
                    agent_id,
                    type,
                    classification,
                    content,
                    embedding,
                    self._jsonb(metadata),
                    retention_until,
                ),
            )
            return self._normalize(cur.fetchone())

    def get(self, memory_id: str) -> dict | None:
        # Default reads exclude soft-deleted rows.
        sql = "SELECT * FROM memories WHERE id = %s AND deleted_at IS NULL"
        with self._cur() as cur:
            cur.execute(sql, (memory_id,))
            return self._normalize(cur.fetchone())

    def update(
        self,
        memory_id: str,
        *,
        content: str | None | object = _UNSET,
        classification: str | object = _UNSET,
        embedding: object = _UNSET,
        metadata: object = _UNSET,
        retention_until: object = _UNSET,
    ) -> dict | None:
        # Column identifiers are static literals from this source file.
        fields: list[str] = []
        params: list[object] = []
        if content is not _UNSET:
            fields.append("content = %s")
            params.append(content)
        if classification is not _UNSET:
            fields.append("classification = %s")
            params.append(classification)
        if embedding is not _UNSET:
            fields.append("embedding = %s")
            params.append(embedding)
        if metadata is not _UNSET:
            fields.append("metadata = %s")
            params.append(self._jsonb(metadata))
        if retention_until is not _UNSET:
            fields.append("retention_until = %s")
            params.append(retention_until)
        if not fields:
            return None
        fields.append("updated_at = now()")
        params.append(memory_id)
        sql = (
            f"UPDATE memories SET {', '.join(fields)} "
            "WHERE id = %s AND deleted_at IS NULL RETURNING *"
        )
        with self._cur() as cur:
            cur.execute(sql, params)
            return self._normalize(cur.fetchone())

    def soft_delete(self, memory_id: str) -> dict | None:
        sql = (
            "UPDATE memories SET deleted_at = now(), updated_at = now() "
            "WHERE id = %s RETURNING *"
        )
        with self._cur() as cur:
            cur.execute(sql, (memory_id,))
            return self._normalize(cur.fetchone())

    def list_by_agent(
        self, agent_id: str, limit: int = 50, offset: int = 0, exclude_secret: bool = False
    ) -> list[dict]:
        sql = (
            "SELECT * FROM memories WHERE agent_id = %s "
            "AND deleted_at IS NULL "
        )
        params = [agent_id]
        if exclude_secret:
            sql += "AND classification != 'secret' "
        sql += "ORDER BY created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        with self._cur() as cur:
            cur.execute(sql, tuple(params))
            return [self._normalize(r) for r in cur.fetchall()]

    def list_by_classification(
        self, classification: str, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        sql = (
            "SELECT * FROM memories WHERE classification = %s "
            "AND deleted_at IS NULL "
            "ORDER BY created_at DESC LIMIT %s OFFSET %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (classification, limit, offset))
            return [self._normalize(r) for r in cur.fetchall()]

    def list_expired(
        self, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        sql = (
            "SELECT * FROM memories "
            "WHERE deleted_at IS NULL AND retention_until IS NOT NULL "
            "AND retention_until < now() "
            "ORDER BY retention_until ASC LIMIT %s OFFSET %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (limit, offset))
            return [self._normalize(r) for r in cur.fetchall()]
