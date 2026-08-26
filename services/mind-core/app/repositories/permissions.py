"""Permission persistence repository (Phase 3).

Groups the RBAC tables ``roles``, ``capabilities``, ``role_capabilities``
and ``permission_grants`` into one repository. Stores relationships only;
makes no authorization decisions.
"""
from __future__ import annotations

from app.repositories.base import BaseRepository


class PermissionRepository(BaseRepository):
    # ── roles ───────────────────────────────────────────────
    def create_role(self, name: str, description: str | None = None) -> dict:
        sql = (
            "INSERT INTO roles (name, description) "
            "VALUES (%s, %s) RETURNING *"
        )
        with self._cur() as cur:
            cur.execute(sql, (name, description))
            return cur.fetchone()

    def get_role(self, role_id: str) -> dict | None:
        sql = "SELECT * FROM roles WHERE id = %s"
        with self._cur() as cur:
            cur.execute(sql, (role_id,))
            return cur.fetchone()

    # ── capabilities ───────────────────────────────────────
    def create_capability(
        self, key: str, description: str | None = None
    ) -> dict:
        sql = (
            "INSERT INTO capabilities (key, description) "
            "VALUES (%s, %s) RETURNING *"
        )
        with self._cur() as cur:
            cur.execute(sql, (key, description))
            return cur.fetchone()

    def get_capability(self, capability_id: str) -> dict | None:
        sql = "SELECT * FROM capabilities WHERE id = %s"
        with self._cur() as cur:
            cur.execute(sql, (capability_id,))
            return cur.fetchone()

    def get_capability_by_key(self, key: str) -> dict | None:
        # Lookup by the natural key (capabilities.key) rather than the
        # surrogate id. The tools.requires_capability FK references
        # capabilities(key), so service-layer validation of a referenced
        # capability key uses this method instead of re-implementing SQL.
        sql = "SELECT * FROM capabilities WHERE key = %s"
        with self._cur() as cur:
            cur.execute(sql, (key,))
            return cur.fetchone()

    # ── role <-> capability mapping ────────────────────────
    def assign_capability_to_role(
        self, role_id: str, capability_id: str
    ) -> None:
        sql = (
            "INSERT INTO role_capabilities (role_id, capability_id) "
            "VALUES (%s, %s) ON CONFLICT DO NOTHING"
        )
        with self._cur() as cur:
            cur.execute(sql, (role_id, capability_id))

    def revoke_capability_from_role(
        self, role_id: str, capability_id: str
    ) -> None:
        sql = (
            "DELETE FROM role_capabilities "
            "WHERE role_id = %s AND capability_id = %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (role_id, capability_id))

    # ── per-agent grants (soft revocation) ─────────────────
    def grant(
        self,
        agent_id: str,
        capability_id: str,
        scope: str = "*",
        granted_by: str | None = None,
        expires_at: object | None = None,
    ) -> dict:
        sql = (
            "INSERT INTO permission_grants "
            "(agent_id, capability_id, scope, granted_by, expires_at) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING *"
        )
        with self._cur() as cur:
            cur.execute(
                sql, (agent_id, capability_id, scope, granted_by, expires_at)
            )
            return cur.fetchone()

    def revoke(self, grant_id: str) -> None:
        # Soft revocation: row is retained for audit; revoked_at set.
        sql = (
            "UPDATE permission_grants SET revoked_at = now() "
            "WHERE id = %s AND revoked_at IS NULL"
        )
        with self._cur() as cur:
            cur.execute(sql, (grant_id,))

    def list_grants_for_agent(
        self, agent_id: str, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        sql = (
            "SELECT * FROM permission_grants WHERE agent_id = %s "
            "ORDER BY granted_at DESC LIMIT %s OFFSET %s"
        )
        with self._cur() as cur:
            cur.execute(sql, (agent_id, limit, offset))
            return cur.fetchall()

    def list_effective_capabilities(self, agent_id: str) -> list[dict]:
        # Role-based capabilities (via the agent's primary role) UNION
        # direct, non-revoked, non-expired grants.
        sql = """
            SELECT c.id, c.key, c.description, 'role' AS source
            FROM capabilities c
            INNER JOIN role_capabilities rc ON rc.capability_id = c.id
            INNER JOIN agents a ON a.primary_role_id = rc.role_id
            WHERE a.id = %s
            UNION
            SELECT c.id, c.key, c.description, 'grant' AS source
            FROM capabilities c
            INNER JOIN permission_grants g ON g.capability_id = c.id
            WHERE g.agent_id = %s
              AND g.revoked_at IS NULL
              AND (g.expires_at IS NULL OR g.expires_at > now())
        """
        with self._cur() as cur:
            cur.execute(sql, (agent_id, agent_id))
            return cur.fetchall()