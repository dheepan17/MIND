"""Permissions HTTP API router (Phase 5.8).

Thin transport adapter over :class:`app.services.permission_service.PermissionService`.

This router is intentionally minimal. It:

* resolves the **trusted** caller identity via :func:`get_security_context`,
  forwarding only ``ctx.actor_id`` / ``ctx.actor_type`` into the service layer
  (never client-supplied body/query actor fields),
* delegates every authorization decision, mutation, and audit record to
  :class:`PermissionService`, which enforces the ``admin:roles`` capability
  gate,
* owns no SQL, no connections, and no transactions -- the request dependency
  (:func:`get_txn` for mutations, :func:`get_conn` for reads) does.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_conn, get_txn
from app.api.schemas.permissions import (
    CapabilityResponse,
    EffectiveCapabilitiesResponse,
    GrantCapabilityRequest,
    GrantResponse,
)
from app.api.security import SecurityContext, get_security_context
from app.services.permission_service import PermissionService

router = APIRouter(prefix="/permissions", tags=["permissions"])

# Reuse the single sanctioned connection entry point. Mutations run inside an
# explicit request transaction (commit/rollback owned by the dependency);
# reads use a plain connection. No route opens its own connection.
Txn = Annotated[object, Depends(get_txn)]
Conn = Annotated[object, Depends(get_conn)]
Ctx = Annotated[SecurityContext, Depends(get_security_context)]


@router.post("/grants", response_model=GrantResponse, status_code=201)
def grant_capability(
    body: GrantCapabilityRequest,
    ctx: Ctx,
    conn: Txn,
) -> dict:
    """Grant a direct capability to an agent.

    Requires ``admin:roles``. ``granted_by`` is the trusted SecurityContext
    actor, never a client-supplied field.
    """
    service = PermissionService(conn)
    return service.grant_capability_admin(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        agent_id=body.agent_id,
        capability_id=body.capability_id,
        scope=body.scope,
        expires_at=body.expires_at,
    )


@router.post("/revoke/{grant_id}", response_model=GrantResponse)
def revoke_capability(
    grant_id: str,
    ctx: Ctx,
    conn: Txn,
) -> dict:
    """Revoke a direct capability grant by id.

    Requires ``admin:roles``. There is no request body; the grant id is the
    path parameter.
    """
    service = PermissionService(conn)
    return service.revoke_capability_admin(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        grant_id=grant_id,
    )


@router.get("/agents/{agent_id}/grants", response_model=list[GrantResponse])
def list_grants_for_agent(
    agent_id: str,
    ctx: Ctx,
    conn: Conn,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    """List direct capability grants for an agent.

    Requires ``admin:roles``. The ``agent_id`` path parameter is the target
    agent, not the authenticated caller.
    """
    service = PermissionService(conn)
    return service.list_grants_for_agent_admin(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        agent_id=agent_id,
        limit=limit,
        offset=offset,
    )


@router.get("/capabilities", response_model=list[CapabilityResponse])
def list_capabilities(
    ctx: Ctx,
    conn: Conn,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    """List registered capabilities.

    Requires ``admin:roles``. Only ``id``, ``key``, and ``description`` are
    exposed; arbitrary database columns are mapped away here.
    """
    service = PermissionService(conn)
    return service.list_capabilities_admin(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        limit=limit,
        offset=offset,
    )


@router.get("/agents/{agent_id}/effective", response_model=EffectiveCapabilitiesResponse)
def get_effective_capabilities(
    agent_id: str,
    ctx: Ctx,
    conn: Conn,
) -> dict:
    """List an agent's effective capabilities.

    Requires ``admin:roles``. The API contract exposes only ``key`` and
    ``source`` per entry; the database surrogate id and description are
    mapped away here.
    """
    service = PermissionService(conn)
    rows = service.get_effective_capabilities_admin(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        agent_id=agent_id,
    )
    return {
        "agent_id": agent_id,
        "capabilities": [
            {"key": row["key"], "source": row["source"]} for row in rows
        ],
    }