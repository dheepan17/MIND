"""EventService — business boundary for the ``events`` table (Phase 4.7 + Phase 6.0).

This is the event-stream service boundary. It owns validation and
existence checks, but contains no SQL and never creates a connection or
commits/rolls back — the caller owns the transaction (see
:class:`app.services.base.BaseService`).

Append-only by design
---------------------
The ``events`` table is an append-only record of what happened in the
system (the event stream). This service therefore exposes **no**
``update`` / ``delete`` / ``remove`` / ``purge`` operations; immutability
is preserved at the service boundary, matching the repository's
append-only contract.

Audit boundary (Phase 6.0)
--------------------------
Per Phase 6.0, event creation **is** mirrored into ``audit_logs`` with
the trusted actor identity and ``capability_used="write"``. The two
stores retain their distinct purposes:

* ``events``  — the event stream / record of what happened.
* ``audit_logs`` — accountability / security audit of *operations*.

Signatures
----------
The schema carries a ``signature`` column (mitigates threat T4). This
service may **accept and persist** a caller-supplied signature verbatim,
but it does **not** generate, verify, or claim validity of any signature.
Cryptographic verification belongs to a future security/crypto boundary.
An unverified signature is stored as-is and never silently transformed
into a verified state.

Authorization boundary (Phase 6.0)
----------------------------------
Event creation is gated on the generic ``write`` capability (catalog:
``read | write | shell | network | data:pii | admin:roles``). No
resource-scoped capability (e.g. ``event:write``) is invented.
Authorization occurs **inside** the caller-owned transaction, **before**
the mutation, via ``PermissionService.require_write(...)`` using the
*trusted* ``SecurityContext.actor_id`` supplied by the router. A missing
``write`` capability raises :class:`AuthorizationError` (HTTP 403); the
denial is recorded in ``audit_logs`` with ``outcome='denied'`` and
``capability_used='write'``.
"""
from __future__ import annotations

import uuid

from app.repositories.agents import AgentRepository
from app.repositories.events import EventRepository
from app.services.audit_service import AuditService
from app.services.base import BaseService
from app.services.errors import AuthorizationError, NotFoundError, ValidationError
from app.services.permission_service import PermissionService

_MAX_LIMIT = 100


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


class EventService(BaseService):
    """Business boundary for the append-only event stream."""

    def __init__(self, conn) -> None:
        super().__init__(conn)
        # PermissionService shares this service's connection and therefore the
        # caller's transaction. It owns the generic ``write`` capability check
        # (Phase 6.0).
        self._perms = PermissionService(conn)
        # AuditService shares this service's connection and therefore the
        # caller's transaction. No connection is created here.
        self._audit = AuditService(conn)

    # ── helpers ───────────────────────────────────────────
    def _require_event(self, event_id: str) -> dict:
        event = self.repository(EventRepository).get(event_id)
        if event is None:
            raise NotFoundError(f"Event '{event_id}' does not exist.")
        return event

    def _require_agent(self, agent_id: str) -> None:
        if self.repository(AgentRepository).get(agent_id) is None:
            raise NotFoundError(f"Agent '{agent_id}' does not exist.")

    @staticmethod
    def _validate_paging(limit: int, offset: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValidationError("limit must be an int.")
        if limit < 1 or limit > _MAX_LIMIT:
            raise ValidationError(
                f"limit must be between 1 and {_MAX_LIMIT}."
            )
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise ValidationError("offset must be an int.")
        if offset < 0:
            raise ValidationError("offset must be >= 0.")

    # ── authorization helpers (Phase 6.0) ──────────────────
    def _require_write(self, actor_id: str) -> None:
        """Gate event creation on the generic ``write`` capability.

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
        action: str = "event.create",
        target_id: str | None = None,
    ) -> None:
        """Record a denied event publication (missing/insufficient capability)."""
        self._audit.record(
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            outcome="denied",
            target_type="event",
            target_id=target_id,
            capability_used="write",
        )

    def _audit_success(
        self,
        *,
        actor_type: str,
        actor_id: str,
        target_id: str,
        context: dict | None = None,
    ) -> None:
        """Record a successful event publication."""
        self._audit.record(
            actor_type=actor_type,
            actor_id=actor_id,
            action="event.create",
            outcome="success",
            target_type="event",
            target_id=target_id,
            capability_used="write",
            context=context,
        )

    # ── 1. create (append) ───────────────────────────────
    def create(
        self,
        *,
        type: str,
        payload: object,
        actor_id: str,
        actor_type: str = "agent",
        producer_agent_id: str | None = None,
        correlation_id: str | None = None,
        signature: str | None = None,
        is_security_event: bool = False,
    ) -> dict:
        """Append a new event to the stream and return the persisted row.

        Validation:
        * ``type`` must be a non-empty string (required).
        * ``payload`` must be provided (the column is JSONB NOT NULL); we
          reject ``None`` rather than letting the database raise a raw
          constraint error.
        * ``correlation_id`` must be a valid UUID when supplied.
        * ``signature`` must be a string when supplied (stored verbatim;
          never generated or verified here).
        * ``is_security_event`` must be a bool.

        Authorization (Phase 6.0):
        * The caller must hold the generic ``write`` capability. The trusted
          ``actor_id`` is supplied by the router (from SecurityContext), never
          from the request body.

        Producer identity (Phase 6.0):
        * ``producer_agent_id`` is overridden by the trusted ``actor_id`` from
          the SecurityContext. A client-supplied ``producer_agent_id`` in the
          request body is ignored for persistence and authorization, preventing
          producer-identity spoofing (threat T4).

        Lower-level database errors are preserved, not swallowed.
        """
        if not isinstance(type, str) or not type:
            raise ValidationError("type must be a non-empty string.")
        if payload is None:
            raise ValidationError("payload is required (JSONB NOT NULL).")
        if correlation_id is not None and not _is_uuid(correlation_id):
            raise ValidationError("correlation_id must be a valid UUID.")
        if signature is not None and (
            not isinstance(signature, str) or isinstance(signature, bool)
        ):
            raise ValidationError("signature must be a string or None.")
        if not isinstance(is_security_event, bool):
            raise ValidationError("is_security_event must be a bool.")

        # Authorization occurs inside the transaction, before the mutation.
        try:
            self._require_write(actor_id)
        except AuthorizationError:
            self._audit_denied(
                actor_type=actor_type,
                actor_id=actor_id,
                action="event.create",
                target_id=None,
            )
            raise

        # Existence check (do not rely on raw FK errors for user input).
        # The producer identity is the trusted actor_id, not the body field.
        trusted_producer = actor_id
        if trusted_producer is not None:
            self._require_agent(trusted_producer)

        # Delegate persistence; no SQL, no connection creation.
        event = self.repository(EventRepository).append(
            type=type,
            producer_agent_id=trusted_producer,
            correlation_id=correlation_id,
            payload=payload,
            signature=signature,
            is_security_event=is_security_event,
        )

        self._audit_success(
            actor_type=actor_type,
            actor_id=actor_id,
            target_id=str(event["id"]),
            context={"type": type, "is_security_event": is_security_event},
        )
        return event

    # ── 2. get ────────────────────────────────────────────
    def get(self, event_id: str) -> dict:
        """Return the event with ``event_id`` or raise :class:`NotFoundError`."""
        if not _is_uuid(event_id):
            raise ValidationError("event_id must be a valid UUID.")
        return self._require_event(event_id)

    # ── 3. list_by_correlation ────────────────────────────
    def list_by_correlation(
        self, correlation_id: str, *, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        """List events sharing ``correlation_id``, newest first."""
        if not _is_uuid(correlation_id):
            raise ValidationError("correlation_id must be a valid UUID.")
        self._validate_paging(limit, offset)
        return self.repository(EventRepository).list_by_correlation(
            correlation_id, limit=limit, offset=offset
        )

    # ── 4. list_by_producer ───────────────────────────────
    def list_by_producer(
        self, producer_agent_id: str, *, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        """List events produced by ``producer_agent_id``, newest first."""
        if not _is_uuid(producer_agent_id):
            raise ValidationError("producer_agent_id must be a valid UUID.")
        self._validate_paging(limit, offset)
        return self.repository(EventRepository).list_by_producer(
            producer_agent_id, limit=limit, offset=offset
        )

    # ── 5. list_security_events ───────────────────────────
    def list_security_events(
        self, *, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        """List security events (``is_security_event = true``), newest first."""
        self._validate_paging(limit, offset)
        return self.repository(EventRepository).list_security_events(
            limit=limit, offset=offset
        )
