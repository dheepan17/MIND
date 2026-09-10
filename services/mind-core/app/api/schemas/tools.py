"""Request/response models for the Tool HTTP API (Phase 5.9).

These are pure transport models. They do NOT duplicate business validation
that belongs to :class:`app.services.tool_service.ToolService`; the service
remains the single source of truth for domain rules. Pydantic is used only
for transport-level parsing/coercion (e.g. required presence, bool typing).

Caller identity (``actor_id``/``actor_type``) is supplied by the trusted
:class:`SecurityContext`, never the request body (Phase 5.7A/5.8).
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class CreateToolRequest(BaseModel):
    """Body for ``POST /tools``.

    Only fields actually accepted by ``ToolService.create_tool`` are present.
    ``requires_capability`` is optional; when supplied it must match a real
    capability key (validated by the service). Caller identity is supplied by
    the trusted SecurityContext, never the request body.
    """

    name: str
    description: str | None = None
    version: str | None = None
    requires_capability: str | None = None
    is_destructive: bool = False
    is_verified: bool = False
    config: dict | None = None
    status: str = "active"


class UpdateToolStatusRequest(BaseModel):
    """Body for ``PATCH /tools/{id}/status``.

    Caller identity is supplied by the trusted SecurityContext, not the body.
    """

    status: str


class MarkVerifiedRequest(BaseModel):
    """Body for ``PATCH /tools/{id}/verified``.

    Caller identity is supplied by the trusted SecurityContext, not the body.
    """

    is_verified: bool


class ToolResponse(BaseModel):
    """Projection of a ``tools`` row returned by the service.

    Mirrors the canonical columns of the ``tools`` table without exposing any
    extra database internals.
    """

    id: UUID
    name: str
    description: str | None = None
    version: str | None = None
    requires_capability: str | None = None
    is_destructive: bool
    is_verified: bool
    config: dict | None = None
    status: str
    created_at: datetime
