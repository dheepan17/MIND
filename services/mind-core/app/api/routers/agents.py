"""Agent HTTP API router (Phase 5.2).

Thin transport adapter over :class:`app.services.agent_service.AgentService`.
It contains no SQL, never instantiates a repository, never opens a connection,
and never commits/rolls back. All business validation, authorization
boundaries, and audit logging live in the service layer.

Transaction ownership is delegated to the request dependency
(:func:`app.api.dependencies.get_txn` for mutations,
:func:`app.api.dependencies.get_conn` for reads), which commits on success
and rolls back if a domain error escapes. Service errors are mapped to HTTP
by the handlers registered in :mod:`app.api.errors`.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_conn, get_txn
from app.api.schemas.agents import (
    AgentResponse,
    CreateAgentRequest,
    UpdateAutonomyRequest,
    UpdateStatusRequest,
)
from app.api.security import SecurityContext, get_security_context
from app.services.agent_service import AgentService

router = APIRouter(prefix="/agents", tags=["agents"])

# Reuse the single sanctioned connection entry point. Mutations run inside an
# explicit request transaction (commit/rollback owned by the dependency);
# reads use a plain connection. No route opens its own connection.
Txn = Annotated[object, Depends(get_txn)]
Conn = Annotated[object, Depends(get_conn)]


@router.post("", response_model=AgentResponse, status_code=201)
def create_agent(body: CreateAgentRequest, conn: Txn) -> dict:
    """Create an agent. Delegates all validation/audit to AgentService."""
    service = AgentService(conn)
    return service.create(**body.model_dump())


@router.get("/{agent_id}", response_model=AgentResponse)
def get_agent(agent_id: str, conn: Conn) -> dict:
    """Return an agent by id (404 if missing; 422 if id is not a UUID)."""
    return AgentService(conn).get(agent_id)


@router.patch("/{agent_id}/status", response_model=AgentResponse)
def update_agent_status(
    agent_id: str, body: UpdateStatusRequest, conn: Txn
) -> dict:
    """Update an agent's status. Domain validation lives in AgentService."""
    service = AgentService(conn)
    return service.update_status(
        agent_id,
        status=body.status,
        actor_type=body.actor_type,
        actor_id=body.actor_id,
    )


@router.patch("/{agent_id}/status/secure", response_model=AgentResponse)
def update_agent_status_secure(
    agent_id: str,
    body: UpdateStatusRequest,
    ctx: Annotated[SecurityContext, Depends(get_security_context)],
    conn: Txn,
) -> dict:
    """Opt-in identity-boundary demo (Phase 5.7A).

    Uses the trusted :class:`SecurityContext` for audit attribution instead of
    the request body's self-asserted ``actor_id``/``actor_type``. The original
    ``PATCH /agents/{agent_id}/status`` route is preserved unchanged for
    backward compatibility. Only this opt-in route requires authentication;
    missing/invalid credentials raise ``AuthenticationError`` -> HTTP 401.
    """
    service = AgentService(conn)
    return service.update_status(
        agent_id,
        status=body.status,
        # Trust the resolver, never the HTTP body.
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
    )


@router.patch("/{agent_id}/autonomy-level", response_model=AgentResponse)
def update_agent_autonomy(
    agent_id: str, body: UpdateAutonomyRequest, conn: Txn
) -> dict:
    """Update an agent's stored autonomy level (interpretation is elsewhere)."""
    service = AgentService(conn)
    return service.update_autonomy_level(
        agent_id,
        autonomy_level=body.autonomy_level,
        actor_type=body.actor_type,
        actor_id=body.actor_id,
    )


@router.get("/by-owner/{owner}", response_model=list[AgentResponse])
def list_agents_by_owner(
    owner: str,
    conn: Conn,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    """List agents owned by ``owner``, newest first."""
    return AgentService(conn).list_by_owner(owner, limit=limit, offset=offset)


@router.get("/by-role/{role_id}", response_model=list[AgentResponse])
def list_agents_by_role(
    role_id: str,
    conn: Conn,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    """List agents whose primary role is ``role_id`` (422 if not a UUID)."""
    return AgentService(conn).list_by_role(role_id, limit=limit, offset=offset)