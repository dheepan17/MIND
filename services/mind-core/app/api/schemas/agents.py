"""Request/response models for the Agent HTTP API (Phase 5.2).

These are pure transport models. They do NOT duplicate business validation
that belongs to :class:`app.services.agent_service.AgentService`; the service
remains the single source of truth for domain rules. Pydantic is used only
for transport-level parsing/coercion (e.g. int fields, required presence).
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID



from pydantic import BaseModel


class CreateAgentRequest(BaseModel):
    """Body for ``POST /agents``.

    Only fields actually accepted by ``AgentService.create`` are present.
    Caller identity (``actor_id``/``actor_type``) is supplied by the trusted
    :class:`SecurityContext`, never the request body (Phase 5.8).
    """

    owner: str
    purpose: str
    autonomy_level: int = 0
    primary_role_id: str | None = None
    status: str = "active"


class UpdateStatusRequest(BaseModel):
    """Body for ``PATCH /agents/{id}/status``.

    Caller identity is supplied by the trusted SecurityContext, not the body.
    """

    status: str


class UpdateAutonomyRequest(BaseModel):
    """Body for ``PATCH /agents/{id}/autonomy-level``.

    Caller identity is supplied by the trusted SecurityContext, not the body.
    """

    autonomy_level: int


class AgentResponse(BaseModel):
    """Projection of an ``agents`` row returned by the service.

    Mirrors the repository's ``RETURNING *`` shape (the canonical columns of
    the ``agents`` table) without exposing any extra database internals.
    """

    id: UUID
    owner: str
    purpose: str
    autonomy_level: int
    primary_role_id: UUID | None = None
    status: str
    created_at: datetime
    updated_at: datetime