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
    ``actor_type`` defaults to ``"agent"`` to match the service default;
    ``actor_id`` is required because ``audit_logs.actor_id`` is NOT NULL.
    ``agent_id`` is optional: when omitted the task is created unassigned
    (the service only enforces existence when a value is supplied).
    """

    title: str
    actor_id: str
    actor_type: str = "agent"
    agent_id: str | None = None
    status: str = "pending"
    priority: int = 5
    parent_task_id: str | None = None
    description: str | None = None
    input_payload: object | None = None
    metadata: object | None = None


class UpdateTaskStatusRequest(BaseModel):
    """Body for ``PATCH /tasks/{id}/status``."""

    status: str
    actor_id: str
    actor_type: str = "agent"


class AssignTaskAgentRequest(BaseModel):
    """Body for ``PATCH /tasks/{id}/assign-agent``.

    ``agent_id`` may be null to unassign the task.
    """

    agent_id: str | None = None
    actor_id: str
    actor_type: str = "agent"


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