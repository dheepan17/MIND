"""Request/response models for the Task HTTP API (Phase 5.3).

These are pure transport models. They do NOT duplicate business validation
that belongs to :class:`app.services.task_service.TaskService`; the service
remains the single source of truth for domain rules. Pydantic is used only
for transport-level parsing/coercion (e.g. int fields, required presence).
"""
from __future__ import annotations
from datetime import datetime
from uuid import UUID


from pydantic import BaseModel


class CreateTaskRequest(BaseModel):
    """Body for ``POST /tasks``.

    Only fields actually accepted by ``TaskService.create`` are present.
    ``agent_id`` is optional: when omitted the task is created unassigned
    (the service only enforces existence when a value is supplied). Caller
    identity (``actor_id``/``actor_type``) is supplied by the trusted
    SecurityContext, never the request body (Phase 5.8).
    """

    title: str
    agent_id: str | None = None
    status: str = "pending"
    priority: int = 5
    parent_task_id: str | None = None
    description: str | None = None
    input_payload: object | None = None
    metadata: object | None = None


class UpdateTaskStatusRequest(BaseModel):
    """Body for ``PATCH /tasks/{id}/status``.

    Caller identity is supplied by the trusted SecurityContext, not the body.
    """

    status: str


class AssignTaskAgentRequest(BaseModel):
    """Body for ``PATCH /tasks/{id}/assign-agent``.

    ``agent_id`` may be null to unassign the task. Caller identity is supplied
    by the trusted SecurityContext, not the body.
    """

    agent_id: str | None = None


class TaskResponse(BaseModel):
    """Projection of a ``tasks`` row returned by the service.

    Mirrors the repository's ``RETURNING *`` shape (the canonical columns of
    the ``tasks`` table) without exposing any extra database internals.
    """

    id: UUID
    parent_task_id: UUID | None = None
    agent_id: UUID | None = None
    title: str
    description: str | None = None
    status: str
    priority: int
    input_payload: object | None = None
    metadata: object | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None