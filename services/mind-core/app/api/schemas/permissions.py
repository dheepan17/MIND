"""Request/response models for the Permissions HTTP API (Phase 5.8).

These are pure transport models. They do NOT duplicate business validation
that belongs to :class:`app.services.permission_service.PermissionService`;
the service remains the single source of truth for domain rules. Pydantic is
used only for transport-level parsing/coercion (e.g. required presence,
datetime typing).

Caller identity (``granted_by``) is supplied by the trusted
:class:`SecurityContext`, never the request body.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class GrantCapabilityRequest(BaseModel):
    """Body for ``POST /permissions/grants``.

    Only fields actually accepted by ``PermissionService`` are present.
    ``granted_by`` is intentionally absent: it must come from the trusted
    SecurityContext actor, never from client-supplied request data.
    """

    agent_id: str
    capability_id: str
    scope: str = "*"
    expires_at: datetime | None = None


class GrantResponse(BaseModel):
    """Projection of a ``permission_grants`` row returned by the service.

    Mirrors the canonical columns of the ``permission_grants`` table without
    exposing any extra database internals. Datetime fields remain typed
    ``datetime`` (never coerced to strings) so clients receive proper
    JSON-schema types.
    """

    id: str
    agent_id: str
    capability_id: str
    scope: str
    granted_by: str | None = None
    granted_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class CapabilityResponse(BaseModel):
    """Projection of a ``capabilities`` row for the API contract.

    Only ``id``, ``key``, and ``description`` are exposed. The repository's
    ``SELECT *`` is intentionally narrowed here so arbitrary database columns
    can never leak outside the API contract.
    """

    id: str
    key: str
    description: str | None = None


class EffectiveCapabilityEntry(BaseModel):
    """A single effective-capability entry for the API contract.

    Exposes only ``key`` and ``source``; the database surrogate id and the
    human-readable ``description`` are intentionally omitted from the API
    response even though the repository may return them internally.
    """

    key: str
    source: str


class EffectiveCapabilitiesResponse(BaseModel):
    """Response for ``GET /permissions/agents/{agent_id}/effective``."""

    agent_id: str
    capabilities: list[EffectiveCapabilityEntry]