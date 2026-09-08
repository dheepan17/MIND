"""Request/response models for the Memory HTTP API (Phase 5.6).

These are pure transport models. They do NOT duplicate business validation
that belongs to :class:`app.services.memory_service.MemoryService`; the
service remains the single source of truth for domain rules. Pydantic is used
for transport-level parsing/coercion with strict enum and length checks so
malformed values are rejected at the boundary (400/422) instead of reaching
PostgreSQL and producing avoidable 500 errors.

The ``memories`` table carries a ``classification`` column
(``public``/``internal``/``secret``). The ``secret`` tier is intentionally
NOT exposed over HTTP in this phase (see the router's policy boundary); the
transport models still enumerate it so a ``secret`` value is rejected with a
clear error rather than silently coerced.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class CreateMemoryRequest(BaseModel):
    """Body for ``POST /memories``.

    Only fields actually accepted by ``MemoryService.create`` are present.
    ``agent_id`` is a required string (the service validates UUID shape and
    agent existence); it is the memory owner, NOT the audit actor. ``type`` and
    ``classification`` use ``Literal`` enums so invalid values are rejected at
    transport time. ``content`` must be non-empty (``min_length=1``). Caller
    identity (``actor_id``/``actor_type``) is supplied by the trusted
    SecurityContext, never the request body (Phase 5.8).
    """

    agent_id: str
    type: Literal["episodic", "semantic", "working"]
    content: str = Field(min_length=1)
    classification: Literal["public", "internal", "secret"] = "internal"
    embedding: list[float] | None = None
    metadata: dict | None = None


class UpdateMemoryRequest(BaseModel):
    """Body for ``PATCH /memories/{id}``.

    All mutable fields are optional; the service treats ``None`` as
    "leave unchanged". ``content`` (when supplied) must be non-empty.
    ``classification`` (when supplied) uses the ``Literal`` enum. Caller
    identity (``actor_id``/``actor_type``) is supplied by the trusted
    SecurityContext, never the request body (Phase 5.8).
    """

    content: str | None = Field(default=None, min_length=1)
    classification: Literal["public", "internal", "secret"] | None = None
    embedding: list[float] | None = None
    metadata: dict | None = None


class DeleteMemoryRequest(BaseModel):
    """Unused schema for ``DELETE /memories/{id}`` (soft delete).

    The DELETE route no longer accepts a body; the audit identity is taken
    from the trusted SecurityContext. Retained for reference/tooling only.
    """


class MemoryResponse(BaseModel):
    """Projection of a ``memories`` row returned by the service.

    Mirrors the repository's ``RETURNING *`` shape (the canonical columns of
    the ``memories`` table) without exposing ``deleted_at`` or any other
    internal repository/soft-delete state.
    """

    id: UUID
    agent_id: UUID | None = None
    type: str
    classification: str
    content: str | None = None
    embedding: list[float] | None = None
    metadata: object | None = None
    created_at: datetime
    updated_at: datetime
