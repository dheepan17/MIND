"""Memory HTTP API router (Phase 5.6).

Thin transport adapter over :class:`app.services.memory_service.MemoryService`.
It contains no SQL, never instantiates a repository, never opens a connection,
and never commits/rolls back. All business validation, audit logging, and
domain transaction participation live in the service layer.

Transaction ownership is delegated to the request dependency
(:func:`app.api.dependencies.get_txn` for mutations,
:func:`app.api.dependencies.get_conn` for reads), which commits on success
and rolls back if a domain error escapes. Service errors are mapped to HTTP
by the handlers registered in :mod:`app.api.errors`.

Secret-classification policy boundary
-------------------------------------
There is currently NO authentication boundary in the HTTP layer, and the
``secret`` classification is metadata only (not an authorization boundary).
To avoid creating a new HTTP exposure for secret memories, this router
rejects at the transport/policy boundary (using the existing
:class:`PolicyViolationError`, mapped to HTTP 403) any request that would
create or escalate a memory to ``secret``, or list ``secret`` memories. The
value is rejected, never silently coerced to ``internal``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_conn, get_txn
from app.api.schemas.memories import (
    CreateMemoryRequest,
    MemoryResponse,
    UpdateMemoryRequest,
)
from app.api.security import SecurityContext, get_security_context
from app.services.errors import PolicyViolationError
from app.services.memory_service import MemoryService

router = APIRouter(prefix="/memories", tags=["memories"])

# Reuse the single sanctioned connection entry point. Mutations run inside an
# explicit request transaction (commit/rollback owned by the dependency);
# reads use a plain connection. No route opens its own connection.
Txn = Annotated[object, Depends(get_txn)]
Conn = Annotated[object, Depends(get_conn)]

# Static collection/list routes are declared before the ``/{memory_id}``
# parameter route so they can never be captured by it.

_SECRET_CLASSIFICATION = "secret"


@router.post("", response_model=MemoryResponse, status_code=201)
def create_memory(
    body: CreateMemoryRequest,
    ctx: Annotated[SecurityContext, Depends(get_security_context)],
    conn: Txn,
) -> dict:
    """Create a memory. Delegates all validation/audit to MemoryService.

    Caller identity comes from the trusted SecurityContext (Phase 5.8).
    """
    if body.classification == _SECRET_CLASSIFICATION:
        raise PolicyViolationError("secret classification is not exposed over the HTTP API")
    service = MemoryService(conn)
    return service.create(
        agent_id=body.agent_id,
        type=body.type,
        content=body.content,
        classification=body.classification,
        embedding=body.embedding,
        metadata=body.metadata,
        actor_id=ctx.actor_id,
        actor_type=ctx.actor_type,
    )


@router.get("/by-agent/{agent_id}", response_model=list[MemoryResponse])
def list_memories_by_agent(
    agent_id: str,
    conn: Conn,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    """List memories for an agent (400 if id is not a UUID).

    Cross-agent listing is permitted in this phase; the pre-auth architecture
    has no authenticated identity, so the router cannot enforce ownership.
    """
    return MemoryService(conn).list_by_agent(agent_id, limit=limit, offset=offset)


@router.get("/by-classification/{classification}", response_model=list[MemoryResponse])
def list_memories_by_classification(
    classification: Literal["public", "internal", "secret"],
    conn: Conn,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    """List memories by classification (422 if value is not a known tier).

    ``secret`` is a valid literal but is blocked at the policy boundary: there
    is no authentication layer, so exposing secret content over HTTP is unsafe.
    Invalid tiers (e.g. ``"topsecret"``) are rejected by transport validation
    (422) before reaching PostgreSQL, avoiding a constraint-violation 500.
    """
    if classification == _SECRET_CLASSIFICATION:
        raise PolicyViolationError("secret classification is not exposed over the HTTP API")
    return MemoryService(conn).list_by_classification(classification, limit=limit, offset=offset)


@router.get("/{memory_id}", response_model=MemoryResponse)
def get_memory(memory_id: str, conn: Conn) -> dict:
    """Return a memory by id (404 if missing; 400 if id is not a UUID)."""
    return MemoryService(conn).get(memory_id)


@router.patch("/{memory_id}", response_model=MemoryResponse)
def update_memory(
    memory_id: str,
    body: UpdateMemoryRequest,
    ctx: Annotated[SecurityContext, Depends(get_security_context)],
    conn: Txn,
) -> dict:
    """Update a memory. Delegates all validation/audit to MemoryService."""
    if body.classification == _SECRET_CLASSIFICATION:
        raise PolicyViolationError("secret classification is not exposed over the HTTP API")
    service = MemoryService(conn)
    return service.update(
        memory_id,
        content=body.content,
        classification=body.classification,
        embedding=body.embedding,
        metadata=body.metadata,
        actor_id=ctx.actor_id,
        actor_type=ctx.actor_type,
    )


@router.delete("/{memory_id}", response_model=MemoryResponse)
def delete_memory(
    memory_id: str,
    ctx: Annotated[SecurityContext, Depends(get_security_context)],
    conn: Txn = None,
) -> dict:
    """Soft-delete a memory. Delegates audit to MemoryService.

    Caller identity comes from the trusted SecurityContext (Phase 5.8); the
    DELETE body/query no longer carries the audit actor. Repeated deletes
    resolve to 404 (the row is already soft-deleted and thus invisible to the
    service's lookup). No hard delete occurs.
    """
    return MemoryService(conn).soft_delete(
        memory_id,
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
    )
