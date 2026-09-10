"""Request/response models for the Event HTTP API (Phase 5.5).

These are pure transport models. They do NOT duplicate business validation
that belongs to :class:`app.services.event_service.EventService`; the service
remains the single source of truth for domain rules. Pydantic is used only
for transport-level parsing/coercion (e.g. required presence). Booleans use
strict validation so only real JSON ``true``/``false`` are accepted; arbitrary
strings like ``"yes"``/``"true"`` and numbers like ``1``/``0`` are rejected.

The ``events`` table carries a ``signature`` column that is stored verbatim
by the service (never generated or verified here). The response model mirrors
the repository's ``RETURNING *`` shape exactly — the canonical columns of the
``events`` table — without exposing any extra database internals.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, StrictBool


class CreateEventRequest(BaseModel):
    """Body for ``POST /events``.

    Only fields actually accepted by ``EventService.create`` are present.
    ``type`` and ``payload`` are required. ``correlation_id`` is an optional
    UUID string; the service validates its shape. ``signature`` is an optional
    verbatim value: any JSON type is accepted at the transport layer and the
    service enforces that it is a string or None (stored verbatim, no
    verification at this layer). Typed as ``object`` so a non-string value
    (e.g. an int) reaches the service and is rejected with a 400 rather than
    being caught by Pydantic's transport validation (422).

    **Producer identity comes from the trusted SecurityContext (Phase 6.0),
    never from this request body.** The service ignores any client-supplied
    ``producer_agent_id``; the stored producer is the authenticated caller.
    """

    type: str
    payload: object
    correlation_id: str | None = None
    signature: object | None = None
    is_security_event: StrictBool = False


class EventResponse(BaseModel):
    """Projection of an ``events`` row returned by the service.

    Mirrors the repository's ``RETURNING *`` shape (the canonical columns of
    the ``events`` table) without exposing any extra database internals.
    """

    id: UUID
    type: str
    producer_agent_id: UUID | None = None
    correlation_id: UUID | None = None
    payload: object
    signature: str | None = None
    is_security_event: bool
    created_at: datetime