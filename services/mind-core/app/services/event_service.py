"""EventService — business boundary for the ``events`` table (Phase 4.7).

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

Audit boundary (intentionally absent)
-------------------------------------
Per the architectural rule, ordinary event creation is **not** mirrored
into ``audit_logs``. The two stores have distinct purposes:

* ``events``  — the event stream / record of what happened.
* ``audit_logs`` — accountability / security audit of *operations*.

Duplicating every event into ``audit_logs`` would conflate the two. If a
future specification explicitly requires auditing for ``EventService``,
that can be added then; we do not invent it here. (See the Phase 4.7
spec and the existing ``AuditService`` boundary.)

Signatures
----------
The schema carries a ``signature`` column (mitigates threat T4). This
service may **accept and persist** a caller-supplied signature verbatim,
but it does **not** generate, verify, or claim validity of any signature.
Cryptographic verification belongs to a future security/crypto boundary.
An unverified signature is stored as-is and never silently transformed
into a verified state.

Authorization boundary
----------------------
The current capability catalog (``read | write | shell | network |
data:pii | admin:roles``) defines **no** event capability (e.g. no
``event:write`` / ``event:read``). Following the same pattern as
``AgentService`` (4.4), ``TaskService`` (4.5) and ``JobService`` (4.6),
this service does NOT gate event operations on a capability. When such
capabilities are introduced, callers can compose
``PermissionService.require_capability(...)`` inside their transaction, or
this service can be extended to require them — no schema change is needed.
We deliberately do not invent an ``event:*`` capability here.
"""
from __future__ import annotations

import uuid

from app.repositories.agents import AgentRepository
from app.repositories.events import EventRepository
from app.services.base import BaseService
from app.services.errors import NotFoundError, ValidationError

_MAX_LIMIT = 100


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


class EventService(BaseService):
    """Business boundary for the append-only event stream."""

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

    # ── 1. create (append) ───────────────────────────────
    def create(
        self,
        *,
        type: str,
        payload: object,
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
        * ``producer_agent_id`` / ``correlation_id`` must be valid UUIDs
          when supplied; a supplied ``producer_agent_id`` must reference a
          real agent (existence check, not a raw FK error).
        * ``signature`` must be a string when supplied (stored verbatim;
          never generated or verified here).
        * ``is_security_event`` must be a bool.

        Lower-level database errors are preserved, not swallowed.
        """
        if not isinstance(type, str) or not type:
            raise ValidationError("type must be a non-empty string.")
        if payload is None:
            raise ValidationError("payload is required (JSONB NOT NULL).")
        if producer_agent_id is not None and not _is_uuid(producer_agent_id):
            raise ValidationError("producer_agent_id must be a valid UUID.")
        if correlation_id is not None and not _is_uuid(correlation_id):
            raise ValidationError("correlation_id must be a valid UUID.")
        if signature is not None and (
            not isinstance(signature, str) or isinstance(signature, bool)
        ):
            raise ValidationError("signature must be a string or None.")
        if not isinstance(is_security_event, bool):
            raise ValidationError("is_security_event must be a bool.")

        # Existence check (do not rely on raw FK errors for user input).
        if producer_agent_id is not None:
            self._require_agent(producer_agent_id)

        # Delegate persistence; no SQL, no connection creation.
        return self.repository(EventRepository).append(
            type=type,
            producer_agent_id=producer_agent_id,
            correlation_id=correlation_id,
            payload=payload,
            signature=signature,
            is_security_event=is_security_event,
        )

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