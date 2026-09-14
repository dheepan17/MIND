"""PermissionService — the authorization boundary for mind-core (Phase 4.2).

This is the first concrete business-logic service. It answers
*authorization* questions (what is an agent allowed to do?) by composing
the existing repositories. It contains no SQL, creates no connections,
and never commits or rolls back — the caller owns the transaction.

Capability model
----------------
An agent's effective capabilities are the UNION of:

* capabilities inherited through the agent's **primary role**
  (``agents.primary_role_id`` -> ``roles`` -> ``role_capabilities`` ->
  ``capabilities``), and
* **active direct grants** (``permission_grants``) where
  ``revoked_at IS NULL`` AND
  (``expires_at IS NULL`` OR ``expires_at > now()``).

The repository layer already computes this UNION; this service wraps it
with existence checks, typed errors, and convenience authorization
helpers (``has_capability`` / ``require_capability``).
"""
from __future__ import annotations

from app.repositories.agents import AgentRepository
from app.repositories.permissions import PermissionRepository
from app.services.audit_service import AuditService
from app.services.base import BaseService
from app.services.errors import AuthorizationError, NotFoundError, ValidationError

# Generic write capability (Phase 5.8 authorization boundary). Per the approved
# Phase 5.7A security policy, resource mutations are gated on this single
# generic capability; no resource-scoped capabilities (agent:create, task:create,
# memory:create, …) are invented.
WRITE_CAPABILITY = "write"

# Administrative capability (Phase 5.8). Every grant/revoke/list/effective
# operation on the permission data is gated on this single capability. No new
# capability names are introduced; this key already exists in the catalog
# (read | write | shell | network | data:pii | admin:roles).
ADMIN_CAPABILITY = "admin:roles"

# PII access capability (Phase 6.1). Memory mutations that create or update a
# ``secret``-classified memory are gated on this capability. The key already
# exists in the catalog; no new capability is invented.
DATA_PII_CAPABILITY = "data:pii"

# Pagination ceiling for Phase 5.8 administrative list endpoints. Kept
# consistent with the rest of the API surface.
_MAX_LIMIT = 100


class PermissionService(BaseService):
    """Authorization boundary for agents, roles, and capabilities.

    Inherits the injected connection from :class:`BaseService`. Builds the
    permission and (when needed) agent repositories on that same
    connection so all work shares the caller's transaction.
    """

    def __init__(self, conn) -> None:
        super().__init__(conn)
        # Repositories share the injected connection and therefore the
        # caller's transaction scope. No connection is created here.
        self._permissions = PermissionRepository(conn)
        self._agents = AgentRepository(conn)
        # AuditService shares this service's connection and therefore the
        # caller's transaction. No connection is created here.
        self._audit = AuditService(conn)

    # ── existence helpers ───────────────────────────────────
    def _require_agent(self, agent_id: str) -> None:
        """Raise :class:`NotFoundError` if the agent does not exist."""
        if self._agents.get(agent_id) is None:
            raise NotFoundError(f"Agent '{agent_id}' does not exist.")

    # ── 1. effective capabilities ───────────────────────────
    def get_effective_capabilities(self, agent_id: str) -> set[str]:
        """Return the agent's effective capability keys (unique set).

        Combines role-derived capabilities with active direct grants.
        Revoked or expired grants are excluded. A nonexistent agent
        raises :class:`NotFoundError` rather than returning an empty set.
        """
        if not agent_id:
            raise ValidationError("agent_id is required.")
        # Distinguish a missing principal from a principal with no caps.
        self._require_agent(agent_id)
        rows = self._permissions.list_effective_capabilities(agent_id)
        return {row["key"] for row in rows}

    # ── 2. capability check (non-throwing) ──────────────────
    def has_capability(self, agent_id: str, capability: str) -> bool:
        """Return True if the agent currently holds ``capability``.

        A missing agent raises :class:`NotFoundError` (callers must be
        able to tell a nonexistent principal apart from a denied one).
        Matching is exact on the canonical capability key.
        """
        if not agent_id:
            raise ValidationError("agent_id is required.")
        if not capability:
            raise ValidationError("capability is required.")
        # Raises NotFoundError for a missing agent; that is the desired
        # behavior (do not collapse "no such agent" into False).
        effective = self.get_effective_capabilities(agent_id)
        return capability in effective

    # ── 3. capability requirement (throwing) ───────────────
    def require_capability(self, agent_id: str, capability: str) -> None:
        """Raise :class:`AuthorizationError` unless the agent has it.

        A missing agent raises :class:`NotFoundError` (not
        :class:`AuthorizationError`), because the principal itself is
        unknown. The error message identifies the denied capability
        without exposing secrets, credentials, or database details.
        """
        if not self.has_capability(agent_id, capability):
            # has_capability already raised NotFoundError for a missing
            # agent, so reaching here means the agent exists but lacks
            # the capability.
            raise AuthorizationError(
                f"Agent '{agent_id}' is not authorized for capability "
                f"'{capability}'."
            )

    # ── 3b. generic write gate (Phase 5.8) ────────────────
    def require_write(self, agent_id: str) -> None:
        """Raise :class:`AuthorizationError` unless the agent holds ``write``.

        Thin, intent-revealing wrapper over :meth:`require_capability` for the
        generic resource-mutation capability. The agent id is the trusted
        ``SecurityContext.actor_id`` passed down by routers — never a
        client-supplied body/query field.
        """
        self.require_capability(agent_id, WRITE_CAPABILITY)

    # ── 3c. PII access gate (Phase 6.1) ─────────────────────
    def require_data_pii(self, agent_id: str) -> None:
        """Raise :class:`AuthorizationError` unless the agent holds ``data:pii``.

        Thin, intent-revealing wrapper over :meth:`require_capability` for the
        PII-access capability. The agent id is the trusted
        ``SecurityContext.actor_id`` passed down by routers — never a
        client-supplied body/query field.
        """
        self.require_capability(agent_id, DATA_PII_CAPABILITY)

    # ── 4. grant a direct capability ────────────────────────
    def grant_capability(
        self,
        agent_id: str,
        capability_id: str,
        scope: str = "*",
        granted_by: str | None = None,
        expires_at=None,
    ) -> dict:
        """Create a direct capability grant for an agent.

        Validates that the target agent and the capability both exist,
        then delegates persistence to the repository. Authorization of
        the *grant operation itself* is intentionally out of scope here
        (AuditService / admin authorization arrive in later phases).

        Returns the created grant row from the repository.
        """
        if not agent_id:
            raise ValidationError("agent_id is required.")
        if not capability_id:
            raise ValidationError("capability_id is required.")
        if scope is None:
            raise ValidationError("scope must not be None.")

        # Verify the principal exists before touching grants, so a bad
        # agent_id yields NotFoundError instead of a raw FK error.
        self._require_agent(agent_id)

        # Verify the capability exists (repository exposes get by id).
        if self._permissions.get_capability(capability_id) is None:
            raise NotFoundError(
                f"Capability '{capability_id}' does not exist."
            )

        return self._permissions.grant(
            agent_id=agent_id,
            capability_id=capability_id,
            scope=scope,
            granted_by=granted_by,
            expires_at=expires_at,
        )

    # ── 5. revoke a direct grant (soft) ─────────────────────
    def revoke_capability(self, grant_id: str) -> None:
        """Soft-revoke a direct grant by its id.

        Delegates to the repository's soft revocation (``revoked_at`` is
        set; the row is retained for audit). No rows are deleted.
        """
        if not grant_id:
            raise ValidationError("grant_id is required.")
        # The repository performs the UPDATE; a missing grant_id simply
        # affects zero rows (idempotent, non-erroring).
        self._permissions.revoke(grant_id)

    # -- 6. admin authorization gate (Phase 5.8) ------------------------
    def require_admin(self, actor_id: str) -> None:
        """Raise :class:`AuthorizationError` unless the agent holds `admin:roles`.

        Intent-revealing wrapper over :meth:`require_capability` for the
        administrative capability. The actor id is the trusted
        `SecurityContext.actor_id` passed down by routers -- never a
        client-supplied body/query field.
        """
        self.require_capability(actor_id, ADMIN_CAPABILITY)

    # -- validation helpers (Phase 5.8) ------------------------------------
    @staticmethod
    def _validate_paging(limit: int, offset: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValidationError("limit must be an int.")
        if limit < 1 or limit > _MAX_LIMIT:
            raise ValidationError(
                f"limit must be an int between 1 and {_MAX_LIMIT}."
            )
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise ValidationError("offset must be an int.")
        if offset < 0:
            raise ValidationError("offset must be an int >= 0.")

    # -- 7. audit helpers (Phase 5.8) ------------------------------------
    def _audit_denied(
        self,
        *,
        actor_type: str,
        actor_id: str,
        action: str,
        target_type: str | None = None,
        target_id: str | None = None,
    ) -> None:
        self._audit.record(
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            outcome='denied',
            target_type=target_type,
            target_id=target_id,
            capability_used=ADMIN_CAPABILITY,
        )

    # -- 8. grant a direct capability (admin-gated, Phase 5.8) -----------
    def grant_capability_admin(
        self,
        *,
        actor_type: str,
        actor_id: str,
        agent_id: str,
        capability_id: str,
        scope: str = '*',
        expires_at=None,
    ) -> dict:
        if not actor_id:
            raise ValidationError('actor_id is required.')
        if not actor_type:
            raise ValidationError('actor_type is required.')
        if not agent_id:
            raise ValidationError('agent_id is required.')
        if not capability_id:
            raise ValidationError('capability_id is required.')
        if scope is None:
            raise ValidationError('scope must not be None.')

        try:
            self.require_admin(actor_id)
        except AuthorizationError:
            self._audit_denied(
                actor_type=actor_type,
                actor_id=actor_id,
                action='permission.grant',
                target_type='agent',
                target_id=agent_id,
            )
            raise

        self._require_agent(agent_id)
        if self._permissions.get_capability(capability_id) is None:
            raise NotFoundError(
                f'Capability {capability_id!r} does not exist.'
            )

        grant = self._permissions.grant(
            agent_id=agent_id,
            capability_id=capability_id,
            scope=scope,
            granted_by=actor_id,
            expires_at=expires_at,
        )
        self._audit.record(
            actor_type=actor_type,
            actor_id=actor_id,
            action='permission.grant',
            outcome='success',
            target_type='permission_grant',
            target_id=grant['id'],
            capability_used=ADMIN_CAPABILITY,
            context={
                'agent_id': agent_id,
                'capability_id': capability_id,
                'scope': scope,
            },
        )
        return grant

    # -- 9. revoke a direct grant (admin-gated, Phase 5.8) ----------------
    def revoke_capability_admin(
        self,
        *,
        actor_type: str,
        actor_id: str,
        grant_id: str,
    ) -> dict:
        if not actor_id:
            raise ValidationError('actor_id is required.')
        if not actor_type:
            raise ValidationError('actor_type is required.')
        if not grant_id:
            raise ValidationError('grant_id is required.')

        try:
            self.require_admin(actor_id)
        except AuthorizationError:
            self._audit_denied(
                actor_type=actor_type,
                actor_id=actor_id,
                action='permission.revoke',
                target_type='permission_grant',
                target_id=grant_id,
            )
            raise

        existing = self._permissions.get_grant(grant_id)
        if existing is None:
            raise NotFoundError(f"Grant '{grant_id}' does not exist.")

        self._permissions.revoke(grant_id)
        self._audit.record(
            actor_type=actor_type,
            actor_id=actor_id,
            action='permission.revoke',
            outcome='success',
            target_type='permission_grant',
            target_id=grant_id,
            capability_used=ADMIN_CAPABILITY,
            context={'agent_id': existing['agent_id']},
        )
        return self._permissions.get_grant(grant_id)

    # -- 10. list grants for an agent (admin-gated, Phase 5.8) ------------
    def list_grants_for_agent_admin(
        self,
        *,
        actor_type: str,
        actor_id: str,
        agent_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        if not actor_id:
            raise ValidationError('actor_id is required.')
        if not actor_type:
            raise ValidationError('actor_type is required.')
        if not agent_id:
            raise ValidationError('agent_id is required.')
        self._require_agent(agent_id)
        self._validate_paging(limit, offset)
        self.require_admin(actor_id)
        return self._permissions.list_grants_for_agent(
            agent_id, limit=limit, offset=offset
        )

    # -- 11. list capabilities (admin-gated, Phase 5.8) -------------------
    def list_capabilities_admin(
        self,
        *,
        actor_type: str,
        actor_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        if not actor_id:
            raise ValidationError('actor_id is required.')
        if not actor_type:
            raise ValidationError('actor_type is required.')
        self._validate_paging(limit, offset)
        self.require_admin(actor_id)
        return self._permissions.list_capabilities(limit=limit, offset=offset)

    # -- 12. effective capabilities (admin-gated, Phase 5.8) --------------
    def get_effective_capabilities_admin(
        self,
        *,
        actor_type: str,
        actor_id: str,
        agent_id: str,
    ) -> list[dict]:
        if not actor_id:
            raise ValidationError('actor_id is required.')
        if not actor_type:
            raise ValidationError('actor_type is required.')
        if not agent_id:
            raise ValidationError('agent_id is required.')
        self._require_agent(agent_id)
        self.require_admin(actor_id)
        return self._permissions.list_effective_capabilities(agent_id)
