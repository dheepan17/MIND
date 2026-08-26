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
    ``agent_id`` and ``actor_id`` are required strings (the service validates
    UUID shape and agent existence). ``type`` and ``classification`` use
    ``Literal`` enums so invalid values are rejected at transport time.
    ``content`` must be non-empty (``min_length=1``). ``actor_type`` defaults
    to ``"agent"`` to match the service default; ``actor_id`` is required
    because ``audit_logs.actor_id`` is NOT NULL.
    """

    agent_id: str
    type: Literal["episodic", "semantic", "working"]
    content: str = Field(min_length=1)
    classification: Literal["public", "internal", "secret"] = "internal"
    embedding: list[float] | None = None
    metadata: dict | None = None
    actor_type: Literal["agent", "human", "role"] = "agent"
    actor_id: str


class UpdateMemoryRequest(BaseModel):
    """Body for ``PATCH /memories/{id}``.

    All mutable fields are optional; the service treats ``None`` as
    "leave unchanged". ``content`` (when supplied) must be non-empty.
    ``classification`` (when supplied) uses the ``Literal`` enum. ``actor_id``
    is required because ``audit_logs.actor_id`` is NOT NULL and the service has
    no default for updates.
    """

    content: str | None = Field(default=None, min_length=1)
    classification: Literal["public", "internal", "secret"] | None = None
    embedding: list[float] | None = None
    metadata: dict | None = None
    actor_type: Literal["agent", "human", "role"] = "agent"
    actor_id: str


class DeleteMemoryRequest(BaseModel):
    """Body for ``DELETE /memories/{id}`` (soft delete).

    ``MemoryService.soft_delete`` requires an ``actor_id`` (the audit log's
    NOT NULL principal) and accepts an ``actor_type`` (default ``"agent"``).
    No other fields are relevant to a soft delete.
    """

    actor_type: Literal["agent", "human", "role"] = "agent"
    actor_id: str


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
