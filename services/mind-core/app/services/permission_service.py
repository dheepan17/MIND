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
from app.services.base import BaseService
from app.services.errors import AuthorizationError, NotFoundError, ValidationError

# Generic write capability (Phase 5.8 authorization boundary). Per the approved
# Phase 5.7A security policy, resource mutations are gated on this single
# generic capability; no resource-scoped capabilities (agent:create, task:create,
# memory:create, …) are invented.
WRITE_CAPABILITY = "write"


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