"""AgentService — business boundary for agent operations (Phase 4.4).

Wraps :class:`AgentRepository` with validation, typed errors, and audit
logging. It contains no SQL, creates no connections, and never commits or
rolls back — the caller owns the transaction (see :class:`BaseService`).

Authorization boundary
----------------------
Per the approved Phase 5.7A security policy, every agent *mutation* is gated
on the generic ``write`` capability (catalog: ``read | write | shell |
network | data:pii | admin:roles``). No resource-scoped capability (e.g.
``agent:create`` / ``agent:manage``) is invented. Authorization occurs
**inside** the caller-owned transaction, **before** the mutation, via
``PermissionService.require_write(...)`` using the *trusted*
``SecurityContext.actor_id`` supplied by the router. A missing ``write``
capability raises :class:`AuthorizationError` (HTTP 403); the denial is
recorded in ``audit_logs`` with ``outcome='denied'`` and
``capability_used='write'``. ``data:pii`` enforcement and read/POST
``/events`` authorization are explicitly out of scope for this phase.

Audit actor
-----------
``audit_logs.actor_id`` is ``NOT NULL`` (schema 0001), so every audited
method requires the caller to identify the principal via ``actor_type``
(``agent`` | ``human`` | ``role``) and ``actor_id``. The service never
invents an actor identity.
"""
from __future__ import annotations

import uuid

from app.repositories.agents import AgentRepository
from app.services.audit_service import AuditService
from app.services.base import BaseService
from app.services.errors import AuthorizationError, NotFoundError, ValidationError
from app.services.permission_service import PermissionService

_VALID_STATUSES = ("active", "suspended", "retired")
_VALID_ACTOR_TYPES = ("agent", "human", "role")
_MIN_AUTONOMY = 0
_MAX_AUTONOMY = 2
_MAX_LIMIT = 100


def _require_uuid(value, field: str) -> str:
    """Validate a non-empty, well-formed UUID (str or uuid.UUID)."""
    if isinstance(value, uuid.UUID):
        value = str(value)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} is required.")
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise ValidationError(f"{field} must be a valid UUID.") from None
    return value


def _require_nonempty(value: str, field: str) -> str:
    """Validate a required, non-empty string field."""
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} is required.")
    return value


class AgentService(BaseService):
    """Business boundary for creating, reading, and mutating agents.

    Inherits the injected connection from :class:`BaseService`. Builds the
    agent repository and an :class:`AuditService` on that same connection so
    all work shares the caller's transaction. No connection is created and
    no transaction is committed or rolled back here.
    """

    def __init__(self, conn) -> None:
        super().__init__(conn)
        # AuditService shares this service's connection and therefore the
        # caller's transaction. No connection is created here.
        self._audit = AuditService(conn)
        # PermissionService shares the same connection/transaction. It owns the
        # generic ``write`` capability check (Phase 5.8).
        self._perms = PermissionService(conn)

    # ── authorization helpers (Phase 5.8) ───────────────────
    def _require_write(self, actor_id: str) -> None:
        """Gate a mutation on the generic ``write`` capability.

        ``actor_id`` is the trusted ``SecurityContext.actor_id`` supplied by the
        router — never a client-supplied body/query field. Raises
        :class:`AuthorizationError` (HTTP 403) when the capability is absent.
        """
        self._perms.require_write(actor_id)

    def _audit_denied(
        self,
        *,
        actor_type: str,
        actor_id: str,
        action: str,
        target_type: str,
        target_id: str | None,
    ) -> None:
        """Record a denied mutation (missing/insufficient capability)."""
        self._audit.record(
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            outcome="denied",
            target_type=target_type,
            target_id=target_id,
            capability_used="write",
        )

    # ── 1. create ──────────────────────────────────────────
    def create(
        self,
        owner: str,
        purpose: str,
        autonomy_level: int = 0,
        primary_role_id: str | None = None,
        status: str = "active",
        *,
        actor_type: str = "agent",
        actor_id: str,
    ) -> dict:
        """Create an agent and audit the creation.

        ``actor_type``/``actor_id`` identify the principal performing the
        action (required; ``audit_logs.actor_id`` is ``NOT NULL``). They are
        not used for authorization (see module docstring).
        """
        _require_nonempty(owner, "owner")
        _require_nonempty(purpose, "purpose")
        self._validate_autonomy(autonomy_level)
        if primary_role_id is not None:
            _require_uuid(primary_role_id, "primary_role_id")
        self._validate_status(status)
        self._validate_actor(actor_type, actor_id)

        # Authorization occurs inside the transaction, before the mutation.
        try:
            self._require_write(actor_id)
        except AuthorizationError:
            self._audit_denied(
                actor_type=actor_type,
                actor_id=actor_id,
                action="agent.create",
                target_type="agent",
                target_id=None,
            )
            raise

        agent = self.repository(AgentRepository).create(
            owner=owner,
            purpose=purpose,
            autonomy_level=autonomy_level,
            primary_role_id=primary_role_id,
            status=status,
        )
        # Audit within the same caller-owned transaction as the insert.
        self._audit.record(
            actor_type=actor_type,
            actor_id=actor_id,
            action="agent.create",
            outcome="success",
            target_type="agent",
            target_id=agent["id"],
            context={"status": status},
        )
        return agent

    # ── 2. get ─────────────────────────────────────────────
    def get(self, agent_id: str) -> dict:
        """Return the agent with ``agent_id`` or raise :class:`NotFoundError`."""
        _require_uuid(agent_id, "agent_id")
        agent = self.repository(AgentRepository).get(agent_id)
        if agent is None:
            raise NotFoundError(f"Agent '{agent_id}' does not exist.")
        return agent

    # ── 3. update_status ───────────────────────────────────
    def update_status(
        self,
        agent_id: str,
        status: str,
        *,
        actor_type: str = "agent",
        actor_id: str,
    ) -> dict:
        """Update an agent's status and audit the mutation.

        Raises :class:`NotFoundError` if the agent does not exist and
        :class:`ValidationError` for an invalid status.
        """
        _require_uuid(agent_id, "agent_id")
        self._validate_status(status)
        self._validate_actor(actor_type, actor_id)
        agent = self.repository(AgentRepository).get(agent_id)
        if agent is None:
            raise NotFoundError(f"Agent '{agent_id}' does not exist.")
        # Authorization occurs inside the transaction, before the mutation.
        try:
            self._require_write(actor_id)
        except AuthorizationError:
            self._audit_denied(
                actor_type=actor_type,
                actor_id=actor_id,
                action="agent.update_status",
                target_type="agent",
                target_id=agent_id,
            )
            raise
        self.repository(AgentRepository).update_status(agent_id, status)
        self._audit.record(
            actor_type=actor_type,
            actor_id=actor_id,
            action="agent.update_status",
            outcome="success",
            target_type="agent",
            target_id=agent_id,
            context={"status": status},
        )
        # Return the updated row for caller convenience.
        return self.repository(AgentRepository).get(agent_id)

    # ── 4. update_autonomy_level ───────────────────────────
    def update_autonomy_level(
        self,
        agent_id: str,
        autonomy_level: int,
        *,
        actor_type: str = "agent",
        actor_id: str,
    ) -> dict:
        """Update an agent's stored autonomy level and audit the mutation.

        The level is stored only; interpretation belongs to the
        authorization layer. Raises :class:`NotFoundError` for a missing
        agent and :class:`ValidationError` for an out-of-range level.
        """
        _require_uuid(agent_id, "agent_id")
        self._validate_autonomy(autonomy_level)
        self._validate_actor(actor_type, actor_id)
        agent = self.repository(AgentRepository).get(agent_id)
        if agent is None:
            raise NotFoundError(f"Agent '{agent_id}' does not exist.")
        # Authorization occurs inside the transaction, before the mutation.
        try:
            self._require_write(actor_id)
        except AuthorizationError:
            self._audit_denied(
                actor_type=actor_type,
                actor_id=actor_id,
                action="agent.update_autonomy_level",
                target_type="agent",
                target_id=agent_id,
            )
            raise
        self.repository(AgentRepository).update_autonomy_level(
            agent_id, autonomy_level
        )
        self._audit.record(
            actor_type=actor_type,
            actor_id=actor_id,
            action="agent.update_autonomy_level",
            outcome="success",
            target_type="agent",
            target_id=agent_id,
            context={"autonomy_level": autonomy_level},
        )
        return self.repository(AgentRepository).get(agent_id)

    # ── 5. list_by_owner ───────────────────────────────────
    def list_by_owner(
        self, owner: str, *, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        """List agents owned by ``owner``, newest first."""
        _require_nonempty(owner, "owner")
        self._validate_paging(limit, offset)
        return self.repository(AgentRepository).list_by_owner(
            owner, limit=limit, offset=offset
        )

    # ── 6. list_by_role ────────────────────────────────────
    def list_by_role(
        self, role_id: str, *, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        """List agents whose primary role is ``role_id``, newest first."""
        _require_uuid(role_id, "role_id")
        self._validate_paging(limit, offset)
        return self.repository(AgentRepository).list_by_role(
            role_id, limit=limit, offset=offset
        )

    # ── validation helpers ─────────────────────────────────
    @staticmethod
    def _validate_status(status: str) -> None:
        if status not in _VALID_STATUSES:
            raise ValidationError(
                f"status must be one of {_VALID_STATUSES}; got {status!r}."
            )

    @staticmethod
    def _validate_actor(actor_type: str, actor_id: str) -> None:
        if actor_type not in _VALID_ACTOR_TYPES:
            raise ValidationError(
                f"actor_type must be one of {_VALID_ACTOR_TYPES}; "
                f"got {actor_type!r}."
            )
        _require_nonempty(actor_id, "actor_id")

    @staticmethod
    def _validate_autonomy(level: int) -> None:
        # bool is a subclass of int; reject it explicitly.
        if not isinstance(level, int) or isinstance(level, bool):
            raise ValidationError("autonomy_level must be an int.")
        if level < _MIN_AUTONOMY or level > _MAX_AUTONOMY:
            raise ValidationError(
                f"autonomy_level must be between {_MIN_AUTONOMY} and "
                f"{_MAX_AUTONOMY}; got {level}."
            )

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