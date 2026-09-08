"""Task HTTP API router (Phase 5.3).

Thin transport adapter over :class:`app.services.task_service.TaskService`.
It contains no SQL, never instantiates a repository, never opens a connection,
and never commits/rolls back. All business validation, authorization
boundaries, and audit logging live in the service layer.

Transaction ownership is delegated to the request dependency
(:func:`app.api.dependencies.get_txn` for mutations,
:func:`app.api.dependencies.get_conn` for reads), which commits on success
and rolls back if a domain error escapes. Service errors are mapped to HTTP
by the handlers registered in :mod:`app.api.errors`.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_conn, get_txn
from app.api.schemas.tasks import (
    AssignTaskAgentRequest,
    CreateTaskRequest,
    TaskResponse,
    UpdateTaskStatusRequest,
)
from app.api.security import SecurityContext, get_security_context
from app.services.task_service import TaskService

router = APIRouter(prefix="/tasks", tags=["tasks"])

# Reuse the single sanctioned connection entry point. Mutations run inside an
# explicit request transaction (commit/rollback owned by the dependency);
# reads use a plain connection. No route opens its own connection.
Txn = Annotated[object, Depends(get_txn)]
Conn = Annotated[object, Depends(get_conn)]


@router.post("", response_model=TaskResponse, status_code=201)
def create_task(
    body: CreateTaskRequest,
    ctx: Annotated[SecurityContext, Depends(get_security_context)],
    conn: Txn,
) -> dict:
    """Create a task. Delegates all validation/audit to TaskService.

    Caller identity comes from the trusted SecurityContext (Phase 5.8).
    """
    service = TaskService(conn)
    return service.create(
        title=body.title,
        agent_id=body.agent_id,
        status=body.status,
        priority=body.priority,
        parent_task_id=body.parent_task_id,
        description=body.description,
        input_payload=body.input_payload,
        metadata=body.metadata,
        actor_id=ctx.actor_id,
        actor_type=ctx.actor_type,
    )


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(task_id: str, conn: Conn) -> dict:
    """Return a task by id (404 if missing; 400 if id is not a UUID)."""
    return TaskService(conn).get(task_id)


@router.patch("/{task_id}/status", response_model=TaskResponse)
def update_task_status(
    task_id: str,
    body: UpdateTaskStatusRequest,
    ctx: Annotated[SecurityContext, Depends(get_security_context)],
    conn: Txn,
) -> dict:
    """Update a task's status. Domain validation lives in TaskService."""
    service = TaskService(conn)
    return service.update_status(
        task_id,
        status=body.status,
        actor_id=ctx.actor_id,
        actor_type=ctx.actor_type,
    )


@router.patch("/{task_id}/assign-agent", response_model=TaskResponse)
def assign_task_agent(
    task_id: str,
    body: AssignTaskAgentRequest,
    ctx: Annotated[SecurityContext, Depends(get_security_context)],
    conn: Txn,
) -> dict:
    """Assign (or unassign) the agent owning a task. Service owns validation."""
    service = TaskService(conn)
    return service.assign_agent(
        task_id,
        agent_id=body.agent_id,
        actor_id=ctx.actor_id,
        actor_type=ctx.actor_type,
    )


@router.get("/{task_id}/children", response_model=list[TaskResponse])
def list_task_children(
    task_id: str,
    conn: Conn,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    """List direct child tasks of ``task_id`` (400 if id is not a UUID)."""
    return TaskService(conn).list_children(
        task_id, limit=limit, offset=offset
    )


@router.get("/{task_id}/subtree", response_model=list[TaskResponse])
def list_task_subtree(
    task_id: str,
    conn: Conn,
    limit: int = Query(100, ge=1, le=100),
) -> list[dict]:
    """List the full descendant subtree of ``task_id`` (400 if not a UUID)."""
    return TaskService(conn).list_subtree(task_id, limit=limit)


@router.get("/by-agent/{agent_id}", response_model=list[TaskResponse])
def list_tasks_by_agent(
    agent_id: str,
    conn: Conn,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    """List tasks owned by ``agent_id`` (400 if id is not a UUID)."""
    return TaskService(conn).list_by_agent(agent_id, limit=limit, offset=offset)


@router.get("/by-status/{status}", response_model=list[TaskResponse])
def list_tasks_by_status(
    status: str,
    conn: Conn,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    """List tasks in a given status (400 if status is invalid)."""
    return TaskService(conn).list_by_status(status, limit=limit, offset=offset)