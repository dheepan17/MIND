"""Event HTTP API router (Phase 5.5 + Phase 6.0).

Thin transport adapter over :class:`app.services.event_service.EventService`.
It contains no SQL, never instantiates a repository, never opens a connection,
and never commits/rolls back. All business validation, authorization
boundaries, and audit logging live in the service layer.

Transaction ownership is delegated to the request dependency
(:func:`app.api.dependencies.get_txn` for mutations,
:func:`app.api.dependencies.get_conn` for reads), which commits on success
and rolls back if a domain error escapes. Service errors are mapped to HTTP
by the handlers registered in :mod:`app.api.errors`.

Append-only by design
---------------------
Events are immutable: this router exposes only ``POST`` (create) and ``GET``
(read) endpoints. No ``PATCH`` / ``PUT`` / ``DELETE`` routes exist, matching
the service's append-only contract.

Authenticated publishing (Phase 6.0)
-------------------------------------
Event creation requires an authenticated ``SecurityContext`` and the generic
``write`` capability. The producer identity stored in the event row comes
from the trusted ``SecurityContext.actor_id``, never from the request body.
This prevents producer-identity spoofing (threat T4). The client-supplied
``producer_agent_id`` body field is ignored by the service.

Signature policy
----------------
The ``signature`` field is transported verbatim. This router performs no
cryptographic verification, generation, or trust decision based on it.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_conn, get_txn
from app.api.schemas.events import CreateEventRequest, EventResponse
from app.api.security import SecurityContext, get_security_context
from app.services.event_service import EventService

router = APIRouter(prefix="/events", tags=["events"])

# Reuse the single sanctioned connection entry point. Mutations run inside an
# explicit request transaction (commit/rollback owned by the dependency);
# reads use a plain connection. No route opens its own connection.
Txn = Annotated[object, Depends(get_txn)]
Conn = Annotated[object, Depends(get_conn)]


@router.post("", response_model=EventResponse, status_code=201)
def create_event(
    body: CreateEventRequest,
    ctx: Annotated[SecurityContext, Depends(get_security_context)],
    conn: Txn,
) -> dict:
    """Append a new event. Delegates all validation and authorization to EventService.

    Caller identity comes from the trusted SecurityContext (Phase 6.0).
    The producer_agent_id stored in the event is ctx.actor_id, never the
    client-supplied body field.
    """
    service = EventService(conn)
    return service.create(
        type=body.type,
        payload=body.payload,
        actor_id=ctx.actor_id,
        actor_type=ctx.actor_type,
        producer_agent_id=body.producer_agent_id,
        correlation_id=body.correlation_id,
        signature=body.signature,
        is_security_event=body.is_security_event,
    )


@router.get("/security", response_model=list[EventResponse])
def list_security_events(
    conn: Conn,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    """List security events (``is_security_event = true``), newest first.

    Registered before ``/events/{event_id}`` so ``security`` is not captured
    as an event UUID path parameter.
    """
    return EventService(conn).list_security_events(limit=limit, offset=offset)


@router.get("/by-correlation/{correlation_id}", response_model=list[EventResponse])
def list_events_by_correlation(
    correlation_id: str,
    conn: Conn,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    """List events sharing ``correlation_id`` (400 if id is not a UUID)."""
    return EventService(conn).list_by_correlation(
        correlation_id, limit=limit, offset=offset
    )


@router.get("/by-producer/{producer_agent_id}", response_model=list[EventResponse])
def list_events_by_producer(
    producer_agent_id: str,
    conn: Conn,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    """List events produced by ``producer_agent_id`` (400 if not a UUID)."""
    return EventService(conn).list_by_producer(
        producer_agent_id, limit=limit, offset=offset
    )


@router.get("/{event_id}", response_model=EventResponse)
def get_event(event_id: str, conn: Conn) -> dict:
    """Return an event by id (404 if missing; 400 if id is not a UUID)."""
    return EventService(conn).get(event_id)
