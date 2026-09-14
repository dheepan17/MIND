"""MemoryService — business boundary for the ``memories`` table (Phase 4.8 + Phase 6.1).

This is the memory-store service boundary. It owns validation and
existence checks, but contains no SQL and never creates a connection or
commits/rolls back — the caller owns the transaction (see
:class:`app.services.base.BaseService`).

Soft-delete by design
---------------------
The ``memories`` table supports soft deletion via ``deleted_at`` (the
repository's default reads exclude soft-deleted rows). This service
therefore exposes **no** hard ``delete`` / ``purge`` / ``remove``
operation; immutability of history is preserved at the service boundary,
matching the repository's soft-delete contract.

Audit boundary (intentionally present)
-------------------------------------
Unlike :class:`EventService` (which deliberately does *not* mirror events
into ``audit_logs``), memory mutations **are** audited here. The
``memories`` table carries a ``classification`` column (``public`` /
``internal`` / ``secret`` / …) that is explicitly called out in the
:class:`AuditService` docstring as security-sensitive state
("memory classification"). Operations that mutate security-sensitive
state are audited, so ``create`` / ``update`` / ``soft_delete`` each emit
an ``audit_logs`` row. Reads (``get`` / ``list_*``) are not audited,
consistent with :class:`AgentService`.

Authorization boundary
---------------------
Per the approved Phase 5.7A security policy, every memory *mutation* is
gated on the generic ``write`` capability (catalog: ``read | write | shell |
network | data:pii | admin:roles``). No resource-scoped capability (e.g.
``memory:write``) is invented. Authorization occurs **inside** the
caller-owned transaction, **before** the mutation, via
``PermissionService.require_write(...)`` using the *trusted*
``SecurityContext.actor_id`` supplied by the router. A missing ``write``
capability raises :class:`AuthorizationError` (HTTP 403); the denial is
recorded in ``audit_logs`` with ``outcome='denied'`` and
``capability_used='write'``.

Phase 6.1 adds a second gate for ``secret``-classified memories: the
generic ``data:pii`` capability is additionally required. The effective
classification is determined from the request (for create) or from the
request merged with the existing row (for update). A transition to
``secret`` always requires ``data:pii``; a transition away from ``secret``
does not. The ``data:pii`` denial is recorded in ``audit_logs`` with
``capability_used='data:pii'``.

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
from app.repositories.memories import _UNSET, MemoryRepository
from app.services.audit_service import AuditService
from app.services.base import BaseService
from app.services.errors import AuthorizationError, NotFoundError, ValidationError
from app.services.permission_service import PermissionService

_MAX_LIMIT = 100
_VALID_ACTOR_TYPES = ("agent", "human", "role")


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


class MemoryService(BaseService):
    """Business boundary for creating, reading, and mutating memories.

    Inherits the injected connection from :class:`BaseService`. Builds the
    memory repository and an :class:`AuditService` on that same connection
    so all work shares the caller's transaction. No connection is created
    and no transaction is committed or rolled back here.
    """

    def __init__(self, conn) -> None:
        super().__init__(conn)
        # AuditService shares this service's connection and therefore the
        # caller's transaction. No connection is created here.
        self._audit = AuditService(conn)
        # PermissionService shares the same connection/transaction. It owns the
        # generic ``write`` capability check (Phase 5.8).
        self._perms = PermissionService(conn)

    # ── authorization helpers (Phase 5.8 + Phase 6.1) ──────
    def _require_write(self, actor_id: str) -> None:
        """Gate a mutation on the generic ``write`` capability.

        ``actor_id`` is the trusted ``SecurityContext.actor_id`` supplied by the
        router — never a client-supplied body/query field. Raises
        :class:`AuthorizationError` (HTTP 403) when the capability is absent.
        """
        self._perms.require_write(actor_id)

    def _require_data_pii(self, actor_id: str) -> None:
        """Gate a secret-classification mutation on ``data:pii``.

        ``actor_id`` is the trusted ``SecurityContext.actor_id`` supplied by the
        router — never a client-supplied body/query field. Raises
        :class:`AuthorizationError` (HTTP 403) when the capability is absent.
        """
        self._perms.require_data_pii(actor_id)

    def _audit_denied(
        self,
        *,
        actor_type: str,
        actor_id: str,
        action: str,
        target_id: str | None,
        capability_used: str = "write",
    ) -> None:
        """Record a denied memory mutation (missing/insufficient capability)."""
        self._audit.record(
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            outcome="denied",
            target_type="memory",
            target_id=target_id,
            capability_used=capability_used,
        )

    # ── helpers ────────────────────────────────────────────
    def _require_memory(self, memory_id: str) -> dict:
        memory = self.repository(MemoryRepository).get(memory_id)
        if memory is None:
            raise NotFoundError(f"Memory '{memory_id}' does not exist.")
        return memory

    def _require_agent(self, agent_id: str) -> None:
        if self.repository(AgentRepository).get(agent_id) is None:
            raise NotFoundError(f"Agent '{agent_id}' does not exist.")

    @staticmethod
    def _validate_actor(actor_type: str, actor_id: str) -> None:
        if actor_type not in _VALID_ACTOR_TYPES:
            raise ValidationError(
                f"actor_type must be one of {_VALID_ACTOR_TYPES}; "
                f"got {actor_type!r}."
            )
        # audit_logs.actor_id is TEXT NOT NULL (it may be an agent UUID, a
        # role name, or a human identifier), so only non-emptiness is
        # enforced here. The caller owns the meaning of the identifier.
        _require_nonempty(actor_id, "actor_id")

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

    # ── 1. create ─────────────────────────────────────────
    def create(
        self,
        *,
        agent_id: str,
        type: str,
        content: str,
        classification: str = "internal",
        embedding: list | None = None,
        metadata: dict | None = None,
        actor_type: str = "agent",
        actor_id: str | None = None,
    ) -> dict:
        """Create a memory and audit the creation.

        Validation:
        * ``agent_id`` must be a valid UUID referencing a real agent
          (existence check, not a raw FK error).
        * ``type`` must be a non-empty string (required).
        * ``content`` must be a non-empty string (column is ``text NOT NULL``).
        * ``classification`` must be a non-empty string when supplied
          (defaults to ``internal``).
        * ``embedding`` must be a list when supplied (stored as JSONB).
        * ``metadata`` must be a dict when supplied (stored as JSONB).

        ``actor_type``/``actor_id`` identify the principal performing the
        action (required; ``audit_logs.actor_id`` is ``NOT NULL``). They are
        not used for authorization (see module docstring).
        """
        _require_uuid(agent_id, "agent_id")
        _require_nonempty(type, "type")
        _require_nonempty(content, "content")
        if classification is not None:
            _require_nonempty(classification, "classification")
        if embedding is not None and not isinstance(embedding, list):
            raise ValidationError("embedding must be a list or None.")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValidationError("metadata must be a dict or None.")
        # A memory is created by its owning agent when no explicit actor is
        # supplied; audit_logs.actor_id is NOT NULL, so default to agent_id
        # (coerced to str, since agent_id may be a uuid.UUID from the repo).
        if actor_id is None:
            actor_id = str(agent_id)
        self._validate_actor(actor_type, actor_id)

        # Existence check (do not rely on raw FK errors for user input).
        self._require_agent(agent_id)

        # Authorization occurs inside the transaction, before the mutation.
        try:
            self._require_write(actor_id)
        except AuthorizationError:
            self._audit_denied(
                actor_type=actor_type,
                actor_id=actor_id,
                action="memory.create",
                target_id=None,
                capability_used="write",
            )
            raise

        if classification == "secret":
            try:
                self._require_data_pii(actor_id)
            except AuthorizationError:
                self._audit_denied(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    action="memory.create",
                    target_id=None,
                    capability_used="data:pii",
                )
                raise

        memory = self.repository(MemoryRepository).create(
            agent_id=agent_id,
            type=type,
            content=content,
            classification=classification,
            embedding=embedding,
            metadata=metadata,
        )
        # Audit within the same caller-owned transaction as the insert.
        self._audit.record(
            actor_type=actor_type,
            actor_id=actor_id,
            action="memory.create",
            outcome="success",
            target_type="memory",
            target_id=memory["id"],
            context={"type": type, "classification": classification},
        )
        return memory

    # ── 2. get ────────────────────────────────────────────
    def get(self, memory_id: str) -> dict:
        """Return the (non-deleted) memory with ``memory_id`` or raise.

        Raises :class:`ValidationError` for a malformed id and
        :class:`NotFoundError` if the memory does not exist (or has been
        soft-deleted).
        """
        _require_uuid(memory_id, "memory_id")
        return self._require_memory(memory_id)

    # ── 3. update ─────────────────────────────────────────
    def update(
        self,
        memory_id: str,
        *,
        content: str | None = None,
        classification: str | None = None,
        embedding: list | None = None,
        metadata: dict | None = None,
        actor_type: str = "agent",
        actor_id: str,
    ) -> dict:
        """Update mutable fields of a memory and audit the mutation.

        Only the fields passed as non-``None`` are changed (the repository
        treats ``None`` as "leave unchanged"). A call with every field
        ``None`` is a no-op and is returned without auditing. Raises
        :class:`NotFoundError` for a missing memory and
        :class:`ValidationError` for malformed values.
        """
        _require_uuid(memory_id, "memory_id")
        if content is not None:
            _require_nonempty(content, "content")
        if classification is not None:
            _require_nonempty(classification, "classification")
        if embedding is not None and not isinstance(embedding, list):
            raise ValidationError("embedding must be a list or None.")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValidationError("metadata must be a dict or None.")
        self._validate_actor(actor_type, actor_id)

        existing = self._require_memory(memory_id)

        # Map None -> _UNSET so the repository treats it as "leave unchanged"
        # rather than "set to NULL". A call with every field None is a no-op.
        content_arg = _UNSET if content is None else content
        classification_arg = _UNSET if classification is None else classification
        embedding_arg = _UNSET if embedding is None else embedding
        metadata_arg = _UNSET if metadata is None else metadata

        if all(
            v is _UNSET
            for v in (
                content_arg,
                classification_arg,
                embedding_arg,
                metadata_arg,
            )
        ):
            return existing

        # Determine the names of fields being changed, for the success
        # audit context (matches the original field-name audit contract).
        changed = [
            name
            for name, v in (
                ("content", content),
                ("classification", classification),
                ("embedding", embedding),
                ("metadata", metadata),
            )
            if v is not None
        ]

        # Determine the effective resulting classification for authorization.
        # A transition TO secret requires data:pii; a transition AWAY from
        # secret does not. When classification is omitted, the existing
        # classification is preserved.
        effective_classification = (
            classification
            if classification is not None
            else existing["classification"]
        )

        # Authorization occurs inside the transaction, before the mutation.
        try:
            self._require_write(actor_id)
        except AuthorizationError:
            self._audit_denied(
                actor_type=actor_type,
                actor_id=actor_id,
                action="memory.update",
                target_id=memory_id,
                capability_used="write",
            )
            raise

        if effective_classification == "secret":
            try:
                self._require_data_pii(actor_id)
            except AuthorizationError:
                self._audit_denied(
                    actor_type=actor_type,
                    actor_id=actor_id,
                    action="memory.update",
                    target_id=memory_id,
                    capability_used="data:pii",
                )
                raise

        updated = self.repository(MemoryRepository).update(
            memory_id,
            content=content_arg,
            classification=classification_arg,
            embedding=embedding_arg,
            metadata=metadata_arg,
        )
        self._audit.record(
            actor_type=actor_type,
            actor_id=actor_id,
            action="memory.update",
            outcome="success",
            target_type="memory",
            target_id=memory_id,
            context={"fields": changed},
        )
        return updated

    # ── 4. soft_delete ────────────────────────────────────
    def soft_delete(
        self,
        memory_id: str,
        *,
        actor_type: str = "agent",
        actor_id: str,
    ) -> dict:
        """Soft-delete a memory (set ``deleted_at``) and audit the action.

        Raises :class:`NotFoundError` if the memory does not exist. There is
        deliberately no hard-delete API (see module docstring).
        """
        _require_uuid(memory_id, "memory_id")
        self._validate_actor(actor_type, actor_id)
        self._require_memory(memory_id)
        # Authorization occurs inside the transaction, before the mutation.
        try:
            self._require_write(actor_id)
        except AuthorizationError:
            self._audit_denied(
                actor_type=actor_type,
                actor_id=actor_id,
                action="memory.soft_delete",
                target_id=memory_id,
            )
            raise
        updated = self.repository(MemoryRepository).soft_delete(memory_id)
        self._audit.record(
            actor_type=actor_type,
            actor_id=actor_id,
            action="memory.soft_delete",
            outcome="success",
            target_type="memory",
            target_id=memory_id,
            context={},
        )
        return updated

    # ── 5. list_by_agent ──────────────────────────────────
    def list_by_agent(
        self, agent_id: str, *, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        """List non-deleted memories for ``agent_id``, newest first."""
        _require_uuid(agent_id, "agent_id")
        self._validate_paging(limit, offset)
        return self.repository(MemoryRepository).list_by_agent(
            agent_id, limit=limit, offset=offset
        )

    # ── 6. list_by_classification ─────────────────────────
    def list_by_classification(
        self, classification: str, *, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        """List non-deleted memories with ``classification``, newest first."""
        _require_nonempty(classification, "classification")
        self._validate_paging(limit, offset)
        return self.repository(MemoryRepository).list_by_classification(
            classification, limit=limit, offset=offset
        )