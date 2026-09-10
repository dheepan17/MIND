"""Tool HTTP API router (Phase 5.9).

Thin transport adapter over :class:`app.services.tool_service.ToolService`.
It contains no SQL, never instantiates a repository, never opens a connection,
and never commits/rolls back. All business validation, authorization
boundaries, and audit logging live in the service layer.

Transaction ownership is delegated to the request dependency
(:func:`app.api.dependencies.get_txn` for mutations,
:func:`app.api.dependencies.get_conn` for reads), which commits on success
and rolls back if a domain error escapes. Service errors are mapped to HTTP
by the handlers registered in :mod:`app.api.errors`.

Read endpoints are intentionally unauthenticated, consistent with the
existing project convention for read operations. Mutations require the
generic ``write`` capability and a trusted :class:`SecurityContext`.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_conn, get_txn
from app.api.schemas.tools import (
    CreateToolRequest,
    MarkVerifiedRequest,
    ToolResponse,
    UpdateToolStatusRequest,
)
from app.api.security import SecurityContext, get_security_context
from app.services.tool_service import ToolService

router = APIRouter(prefix="/tools", tags=["tools"])

# Reuse the single sanctioned connection entry point. Mutations run inside an
# explicit request transaction (commit/rollback owned by the dependency);
# reads use a plain connection. No route opens its own connection.
Txn = Annotated[object, Depends(get_txn)]
Conn = Annotated[object, Depends(get_conn)]


@router.post("", response_model=ToolResponse, status_code=201)
def create_tool(
    body: CreateToolRequest,
    ctx: Annotated[SecurityContext, Depends(get_security_context)],
    conn: Txn,
) -> dict:
    """Create a tool. Delegates all validation/audit to ToolService.

    Caller identity comes from the trusted SecurityContext (Phase 5.7A/5.8).
    Requires the generic ``write`` capability.
    """
    service = ToolService(conn)
    return service.create_tool(
        name=body.name,
        description=body.description,
        version=body.version,
        requires_capability=body.requires_capability,
        is_destructive=body.is_destructive,
        is_verified=body.is_verified,
        config=body.config,
        status=body.status,
        actor_id=ctx.actor_id,
        actor_type=ctx.actor_type,
    )


@router.get("/destructive", response_model=list[ToolResponse])
def list_destructive_tools(
    conn: Conn,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    """List destructive tools (``is_destructive = true``), ordered by name."""
    return ToolService(conn).list_destructive(limit=limit, offset=offset)


@router.get("/by-capability/{capability_key}", response_model=list[ToolResponse])
def list_tools_by_capability(
    capability_key: str,
    conn: Conn,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    """List tools associated with ``capability_key``, ordered by name."""
    return ToolService(conn).list_by_capability(
        capability_key, limit=limit, offset=offset
    )


@router.get("/{tool_id}", response_model=ToolResponse)
def get_tool(tool_id: str, conn: Conn) -> dict:
    """Return a tool by id (404 if missing; 422 if id is not a UUID)."""
    return ToolService(conn).get_tool(tool_id)


@router.patch("/{tool_id}/status", response_model=ToolResponse)
def update_tool_status(
    tool_id: str,
    body: UpdateToolStatusRequest,
    ctx: Annotated[SecurityContext, Depends(get_security_context)],
    conn: Txn,
) -> dict:
    """Update a tool's status. Domain validation lives in ToolService.

    Requires the generic ``write`` capability and a trusted SecurityContext.
    """
    service = ToolService(conn)
    return service.update_status(
        tool_id,
        status=body.status,
        actor_id=ctx.actor_id,
        actor_type=ctx.actor_type,
    )


@router.patch("/{tool_id}/verified", response_model=ToolResponse)
def mark_tool_verified(
    tool_id: str,
    body: MarkVerifiedRequest,
    ctx: Annotated[SecurityContext, Depends(get_security_context)],
    conn: Txn,
) -> dict:
    """Update a tool's verification state. Domain validation lives in ToolService.

    Requires the generic ``write`` capability and a trusted SecurityContext.
    """
    service = ToolService(conn)
    return service.mark_verified(
        tool_id,
        is_verified=body.is_verified,
        actor_id=ctx.actor_id,
        actor_type=ctx.actor_type,
    )
