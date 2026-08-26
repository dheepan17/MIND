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
    ``actor_type`` defaults to ``"agent"`` to match the service default;
    ``actor_id`` is required because ``audit_logs.actor_id`` is NOT NULL.
    """

    owner: str
    purpose: str
    autonomy_level: int = 0
    primary_role_id: str | None = None
    status: str = "active"
    actor_type: str = "agent"
    actor_id: str


class UpdateStatusRequest(BaseModel):
    """Body for ``PATCH /agents/{id}/status``."""

    status: str
    actor_type: str = "agent"
    actor_id: str


class UpdateAutonomyRequest(BaseModel):
    """Body for ``PATCH /agents/{id}/autonomy-level``."""

    autonomy_level: int
    actor_type: str = "agent"
    actor_id: str


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