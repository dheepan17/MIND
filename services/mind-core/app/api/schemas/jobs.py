"""Request/response models for the Job HTTP API (Phase 5.4).

These are pure transport models. They do NOT duplicate business validation
that belongs to :class:`app.services.job_service.JobService`; the service
remains the single source of truth for domain rules. Pydantic is used only
for transport-level parsing/coercion (e.g. required presence).

The ``jobs`` table has no ``created_at``/``updated_at`` columns (unlike
``tasks``); its lifecycle timestamps are ``scheduled_at``/``started_at``/
``finished_at``. The response model mirrors the repository's ``RETURNING *``
shape exactly.
"""
from __future__ import annotations
from datetime import datetime
from uuid import UUID


from pydantic import BaseModel


class CreateJobRequest(BaseModel):
    """Body for ``POST /jobs``.

    Only fields actually accepted by ``JobService.create`` are present.
    ``actor_type`` defaults to ``"agent"`` to match the service default;
    ``actor_id`` is required because ``audit_logs.actor_id`` is NOT NULL.
    ``task_id``/``agent_id``/``tool_id`` are optional links; the service
    enforces existence only when a value is supplied.
    """

    actor_id: str
    actor_type: str = "agent"
    task_id: str | None = None
    agent_id: str | None = None
    tool_id: str | None = None
    status: str = "queued"
    scheduled_at: object | None = None


class StartJobRequest(BaseModel):
    """Body for ``POST /jobs/{id}/start``."""

    actor_id: str
    actor_type: str = "agent"


class FinishJobRequest(BaseModel):
    """Body for ``POST /jobs/{id}/finish``."""

    result: object | None = None
    actor_id: str
    actor_type: str = "agent"


class FailJobRequest(BaseModel):
    """Body for ``POST /jobs/{id}/fail``."""

    error: str | None = None
    actor_id: str
    actor_type: str = "agent"


class RetryJobRequest(BaseModel):
    """Body for ``POST /jobs/{id}/retry``."""

    actor_id: str
    actor_type: str = "agent"


class JobResponse(BaseModel):
    """Projection of a ``jobs`` row returned by the service.

    Mirrors the repository's ``RETURNING *`` shape (the canonical columns of
    the ``jobs`` table) without exposing any extra database internals.
    """

    id: UUID
    task_id: UUID | None = None
    agent_id: UUID | None = None
    tool_id: UUID | None = None
    status: str
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: object | None = None
    error: str | None = None
    retry_count: int