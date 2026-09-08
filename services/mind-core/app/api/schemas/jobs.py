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
    ``task_id``/``agent_id``/``tool_id`` are optional links; the service
    enforces existence only when a value is supplied. Caller identity
    (``actor_id``/``actor_type``) is supplied by the trusted SecurityContext,
    never the request body (Phase 5.8).
    """

    task_id: str | None = None
    agent_id: str | None = None
    tool_id: str | None = None
    status: str = "queued"
    scheduled_at: object | None = None


class StartJobRequest(BaseModel):
    """Body for ``POST /jobs/{id}/start``.

    Caller identity is supplied by the trusted SecurityContext, not the body.
    """


class FinishJobRequest(BaseModel):
    """Body for ``POST /jobs/{id}/finish``.

    Caller identity is supplied by the trusted SecurityContext, not the body.
    """

    result: object | None = None


class FailJobRequest(BaseModel):
    """Body for ``POST /jobs/{id}/fail``.

    Caller identity is supplied by the trusted SecurityContext, not the body.
    """

    error: str | None = None


class RetryJobRequest(BaseModel):
    """Body for ``POST /jobs/{id}/retry``.

    Caller identity is supplied by the trusted SecurityContext, not the body.
    """


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