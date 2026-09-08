"""Job HTTP API router (Phase 5.4).

Thin transport adapter over :class:`app.services.job_service.JobService`.
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
from app.api.schemas.jobs import (
    CreateJobRequest,
    FailJobRequest,
    FinishJobRequest,
    JobResponse,
    RetryJobRequest,
    StartJobRequest,
)
from app.api.security import SecurityContext, get_security_context
from app.services.job_service import JobService

router = APIRouter(prefix="/jobs", tags=["jobs"])

# Reuse the single sanctioned connection entry point. Mutations run inside an
# explicit request transaction (commit/rollback owned by the dependency);
# reads use a plain connection. No route opens its own connection.
Txn = Annotated[object, Depends(get_txn)]
Conn = Annotated[object, Depends(get_conn)]


@router.post("", response_model=JobResponse, status_code=201)
def create_job(
    body: CreateJobRequest,
    ctx: Annotated[SecurityContext, Depends(get_security_context)],
    conn: Txn,
) -> dict:
    """Create a job. Delegates all validation/audit to JobService.

    Caller identity comes from the trusted SecurityContext (Phase 5.8).
    """
    service = JobService(conn)
    return service.create(
        task_id=body.task_id,
        agent_id=body.agent_id,
        tool_id=body.tool_id,
        status=body.status,
        scheduled_at=body.scheduled_at,
        actor_id=ctx.actor_id,
        actor_type=ctx.actor_type,
    )


@router.get("/{job_id}", response_model=JobResponse)
def get_job(job_id: str, conn: Conn) -> dict:
    """Return a job by id (404 if missing; 400 if id is not a UUID)."""
    return JobService(conn).get(job_id)


@router.post("/{job_id}/start", response_model=JobResponse)
def start_job(
    job_id: str,
    body: StartJobRequest,
    ctx: Annotated[SecurityContext, Depends(get_security_context)],
    conn: Txn,
) -> dict:
    """Mark a job running. Domain validation lives in JobService."""
    service = JobService(conn)
    return service.start(
        job_id,
        actor_id=ctx.actor_id,
        actor_type=ctx.actor_type,
    )


@router.post("/{job_id}/finish", response_model=JobResponse)
def finish_job(
    job_id: str,
    body: FinishJobRequest,
    ctx: Annotated[SecurityContext, Depends(get_security_context)],
    conn: Txn,
) -> dict:
    """Mark a job succeeded. Domain validation lives in JobService."""
    service = JobService(conn)
    return service.finish(
        job_id,
        result=body.result,
        actor_id=ctx.actor_id,
        actor_type=ctx.actor_type,
    )


@router.post("/{job_id}/fail", response_model=JobResponse)
def fail_job(
    job_id: str,
    body: FailJobRequest,
    ctx: Annotated[SecurityContext, Depends(get_security_context)],
    conn: Txn,
) -> dict:
    """Mark a job failed. Domain validation lives in JobService."""
    service = JobService(conn)
    return service.fail(
        job_id,
        error=body.error,
        actor_id=ctx.actor_id,
        actor_type=ctx.actor_type,
    )


@router.post("/{job_id}/retry", response_model=JobResponse)
def retry_job(
    job_id: str,
    body: RetryJobRequest,
    ctx: Annotated[SecurityContext, Depends(get_security_context)],
    conn: Txn,
) -> dict:
    """Re-queue a failed job. Domain validation lives in JobService."""
    service = JobService(conn)
    return service.retry(
        job_id,
        actor_id=ctx.actor_id,
        actor_type=ctx.actor_type,
    )


@router.get("/by-task/{task_id}", response_model=list[JobResponse])
def list_jobs_by_task(
    task_id: str,
    conn: Conn,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    """List jobs for a task (400 if id is not a UUID)."""
    return JobService(conn).list_by_task(task_id, limit=limit, offset=offset)


@router.get("/by-agent/{agent_id}", response_model=list[JobResponse])
def list_jobs_by_agent(
    agent_id: str,
    conn: Conn,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    """List jobs for an agent (400 if id is not a UUID)."""
    return JobService(conn).list_by_agent(agent_id, limit=limit, offset=offset)


@router.get("/by-status/{status}", response_model=list[JobResponse])
def list_jobs_by_status(
    status: str,
    conn: Conn,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    """List jobs in a given status (400 if status is invalid)."""
    return JobService(conn).list_by_status(status, limit=limit, offset=offset)