"""JobService — business boundary for the ``jobs`` table (Phase 4.6).

This is the job-lifecycle service boundary. It owns validation,
existence checks, and audit integration, but contains no SQL and never
creates a connection or commits/rolls back — the caller owns the
transaction (see :class:`app.services.base.BaseService`).

Authorization boundary
----------------------
The current capability catalog (``read | write | shell | network |
data:pii | admin:roles``) defines **no** *resource-scoped* job capability
(e.g. ``job:create`` / ``job:run``). Per the approved Phase 5.7A security
policy, every job *mutation* is instead gated on the generic ``write``
capability. Authorization occurs **inside** the caller-owned transaction,
**before** the mutation, via ``PermissionService.require_write(...)`` using
the *trusted* ``SecurityContext.actor_id`` supplied by the router. A missing
``write`` capability raises :class:`AuthorizationError` (HTTP 403); the denial
is recorded in ``audit_logs`` with ``outcome='denied'`` and
``capability_used='write'``. No resource-scoped ``job:*`` capability is invented.

Schema notes (migration 0001)
-----------------------------
The ``jobs`` table has **no** ``kind`` column. Job records are typed
only by their ``status`` enum (``queued | running | succeeded | failed |
cancelled``) and by the optional ``task_id`` / ``agent_id`` / ``tool_id``
foreign keys. This service therefore exposes no ``kind`` parameter and
invents no job-kind enum.

Timestamps and retry_count are repository-owned:
* ``mark_started`` sets ``status='running'`` and ``started_at=now()``.
* ``mark_finished`` sets ``status='succeeded'``, ``finished_at=now()``,
  and persists ``result`` (JSONB).
* ``mark_failed`` sets ``status='failed'``, ``finished_at=now()``, and
  persists ``error`` (TEXT).
* ``increment_retry`` performs ``retry_count = retry_count + 1``
  atomically in SQL. The service does NOT recompute the counter.

Audit redaction
---------------
``job.result`` (JSONB) and ``job.error`` (TEXT) may contain sensitive
payloads. Per the audit data policy (``audit_logs.context`` is redacted),
the audit context for lifecycle mutations contains only the resulting
``status`` — never the result or error bodies.
"""
from __future__ import annotations

import uuid

from app.repositories.agents import AgentRepository
from app.repositories.jobs import JobRepository
from app.repositories.tasks import TaskRepository
from app.repositories.tools import ToolRepository
from app.services.audit_service import AuditService
from app.services.base import BaseService
from app.services.errors import AuthorizationError, NotFoundError, ValidationError
from app.services.permission_service import PermissionService

_VALID_STATUSES = (
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelled",
)
_VALID_ACTOR_TYPES = ("agent", "human", "role")
_MAX_LIMIT = 100


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


class JobService(BaseService):
    """Business boundary for job lifecycle operations."""

    def __init__(self, conn) -> None:
        super().__init__(conn)
        # PermissionService shares this service's connection and therefore the
        # caller's transaction. It owns the generic ``write`` capability check
        # (Phase 5.8).
        self._perms = PermissionService(conn)

    # ── authorization helpers (Phase 5.8) ───────────────────
    def _require_write(self, actor_id: str) -> None:
        """Gate a mutation on the generic ``write`` capability.

        ``actor_id`` is the trusted ``SecurityContext.actor_id`` supplied by the
        router — never a client-supplied body/query field. Raises
        :class:`AuthorizationError` (HTTP 403) when the capability is absent.
        """
        self._perms.require_write(actor_id)

    def _audit_denied(
        self,
        *,
        actor_type: str,
        actor_id: str,
        action: str,
        target_id: str | None,
    ) -> None:
        """Record a denied job mutation (missing/insufficient capability)."""
        AuditService(self._conn).record(
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            outcome="denied",
            target_type="job",
            target_id=target_id,
            capability_used="write",
        )

    # ── helpers ───────────────────────────────────────────
    def _require_job(self, job_id: str) -> dict:
        job = self.repository(JobRepository).get(job_id)
        if job is None:
            raise NotFoundError(f"Job '{job_id}' does not exist.")
        return job

    def _require_task(self, task_id: str) -> None:
        if self.repository(TaskRepository).get(task_id) is None:
            raise NotFoundError(f"Task '{task_id}' does not exist.")

    def _require_agent(self, agent_id: str) -> None:
        if self.repository(AgentRepository).get(agent_id) is None:
            raise NotFoundError(f"Agent '{agent_id}' does not exist.")

    def _require_tool(self, tool_id: str) -> None:
        if self.repository(ToolRepository).get(tool_id) is None:
            raise NotFoundError(f"Tool '{tool_id}' does not exist.")

    @staticmethod
    def _validate_paging(limit: int, offset: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValidationError("limit must be an int.")
        if limit < 1 or limit > _MAX_LIMIT:
            raise ValidationError(f"limit must be between 1 and {_MAX_LIMIT}.")
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise ValidationError("offset must be an int.")
        if offset < 0:
            raise ValidationError("offset must be >= 0.")

    def _audit(
        self,
        *,
        actor_type: str,
        actor_id: str | None,
        action: str,
        target_id: str | None,
        context: dict | None = None,
    ) -> None:
        # AuditService validates actor_type/outcome and persists on the
        # same caller-owned connection. Never raises for a missing
        # actor_id (the column is NOT NULL but the caller is responsible
        # for supplying it; we pass it through unchanged).
        AuditService(self._conn).record(
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            outcome="success",
            target_type="job",
            target_id=target_id,
            context=context,
        )

    # ── 1. create ─────────────────────────────────────────
    def create(
        self,
        *,
        actor_id: str,
        actor_type: str = "agent",
        task_id: str | None = None,
        agent_id: str | None = None,
        tool_id: str | None = None,
        status: str = "queued",
        scheduled_at: object | None = None,
    ) -> dict:
        if actor_type not in _VALID_ACTOR_TYPES:
            raise ValidationError(
                f"actor_type must be one of {_VALID_ACTOR_TYPES}."
            )
        if status not in _VALID_STATUSES:
            raise ValidationError(
                f"status must be one of {_VALID_STATUSES}; got {status!r}."
            )
        if task_id is not None and not _is_uuid(task_id):
            raise ValidationError("task_id must be a valid UUID.")
        if agent_id is not None and not _is_uuid(agent_id):
            raise ValidationError("agent_id must be a valid UUID.")
        if tool_id is not None and not _is_uuid(tool_id):
            raise ValidationError("tool_id must be a valid UUID.")

        # Existence checks (do not rely on raw FK errors for user input).
        if task_id is not None:
            self._require_task(task_id)
        if agent_id is not None:
            self._require_agent(agent_id)
        if tool_id is not None:
            self._require_tool(tool_id)

        # Authorization occurs inside the transaction, before the mutation.
        try:
            self._require_write(actor_id)
        except AuthorizationError:
            self._audit_denied(
                actor_type=actor_type,
                actor_id=actor_id,
                action="job.create",
                target_id=None,
            )
            raise

        job = self.repository(JobRepository).create(
            task_id=task_id,
            agent_id=agent_id,
            tool_id=tool_id,
            status=status,
            scheduled_at=scheduled_at,
        )
        self._audit(
            actor_type=actor_type,
            actor_id=actor_id,
            action="job.create",
            target_id=str(job["id"]),
            context={"status": status},
        )
        return job

    # ── 2. get ────────────────────────────────────────────
    def get(self, job_id: str) -> dict:
        if not _is_uuid(job_id):
            raise ValidationError("job_id must be a valid UUID.")
        return self._require_job(job_id)

    # ── 3. mark_started ──────────────────────────────────
    def mark_started(
        self,
        job_id: str,
        *,
        actor_id: str,
        actor_type: str = "agent",
    ) -> dict:
        if not _is_uuid(job_id):
            raise ValidationError("job_id must be a valid UUID.")
        if actor_type not in _VALID_ACTOR_TYPES:
            raise ValidationError(
                f"actor_type must be one of {_VALID_ACTOR_TYPES}."
            )
        self._require_job(job_id)
        # Authorization occurs inside the transaction, before the mutation.
        try:
            self._require_write(actor_id)
        except AuthorizationError:
            self._audit_denied(
                actor_type=actor_type,
                actor_id=actor_id,
                action="job.mark_started",
                target_id=job_id,
            )
            raise
        # Repository owns status='running' and started_at=now().
        self.repository(JobRepository).mark_started(job_id)
        self._audit(
            actor_type=actor_type,
            actor_id=actor_id,
            action="job.mark_started",
            target_id=job_id,
            context={"status": "running"},
        )
        return self._require_job(job_id)

    # ── 4. mark_finished ──────────────────────────────────
    def mark_finished(
        self,
        job_id: str,
        result: object | None,
        *,
        actor_id: str,
        actor_type: str = "agent",
    ) -> dict:
        if not _is_uuid(job_id):
            raise ValidationError("job_id must be a valid UUID.")
        if actor_type not in _VALID_ACTOR_TYPES:
            raise ValidationError(
                f"actor_type must be one of {_VALID_ACTOR_TYPES}."
            )
        self._require_job(job_id)
        # Authorization occurs inside the transaction, before the mutation.
        try:
            self._require_write(actor_id)
        except AuthorizationError:
            self._audit_denied(
                actor_type=actor_type,
                actor_id=actor_id,
                action="job.mark_finished",
                target_id=job_id,
            )
            raise
        # Repository owns status='succeeded', finished_at=now(), result.
        # The result body is intentionally NOT placed in the audit context
        # (redaction policy).
        self.repository(JobRepository).mark_finished(job_id, result)
        self._audit(
            actor_type=actor_type,
            actor_id=actor_id,
            action="job.mark_finished",
            target_id=job_id,
            context={"status": "succeeded"},
        )
        return self._require_job(job_id)

    # ── 5. mark_failed ────────────────────────────────────
    def mark_failed(
        self,
        job_id: str,
        error: str | None,
        *,
        actor_id: str,
        actor_type: str = "agent",
    ) -> dict:
        if not _is_uuid(job_id):
            raise ValidationError("job_id must be a valid UUID.")
        if actor_type not in _VALID_ACTOR_TYPES:
            raise ValidationError(
                f"actor_type must be one of {_VALID_ACTOR_TYPES}."
            )
        if error is not None and not isinstance(error, str):
            raise ValidationError("error must be a string or None.")
        self._require_job(job_id)
        # Authorization occurs inside the transaction, before the mutation.
        try:
            self._require_write(actor_id)
        except AuthorizationError:
            self._audit_denied(
                actor_type=actor_type,
                actor_id=actor_id,
                action="job.mark_failed",
                target_id=job_id,
            )
            raise
        # Repository owns status='failed', finished_at=now(), error.
        # The error body is intentionally NOT placed in the audit context
        # (redaction policy).
        self.repository(JobRepository).mark_failed(job_id, error)
        self._audit(
            actor_type=actor_type,
            actor_id=actor_id,
            action="job.mark_failed",
            target_id=job_id,
            context={"status": "failed"},
        )
        return self._require_job(job_id)
        # ── HTTP/API lifecycle aliases ─────────────────────────────────────────

    def start(
        self,
        job_id: str,
        *,
        actor_id: str,
        actor_type: str = "agent",
    ) -> dict:
        """Start a job and transition it to ``running``."""
        return self.mark_started(
            job_id,
            actor_id=actor_id,
            actor_type=actor_type,
        )

    def finish(
        self,
        job_id: str,
        result: object | None,
        *,
        actor_id: str,
        actor_type: str = "agent",
    ) -> dict:
        """Finish a job successfully and persist its result."""
        return self.mark_finished(
            job_id,
            result,
            actor_id=actor_id,
            actor_type=actor_type,
        )

    def fail(
        self,
        job_id: str,
        error: str | None,
        *,
        actor_id: str,
        actor_type: str = "agent",
    ) -> dict:
        """Mark a job as failed and persist its error."""
        return self.mark_failed(
            job_id,
            error,
            actor_id=actor_id,
            actor_type=actor_type,
        )

    def retry(
        self,
        job_id: str,
        *,
        actor_id: str,
        actor_type: str = "agent",
    ) -> dict:
        """Re-queue a failed job and increment its retry counter."""

        if not _is_uuid(job_id):
            raise ValidationError("job_id must be a valid UUID.")

        if actor_type not in _VALID_ACTOR_TYPES:
            raise ValidationError(
                f"actor_type must be one of {_VALID_ACTOR_TYPES}."
            )

        job = self._require_job(job_id)

        if job["status"] != "failed":
            raise ValidationError(
                "Only failed jobs can be retried."
            )

        # Authorization occurs inside the transaction, before the mutation.
        try:
            self._require_write(actor_id)
        except AuthorizationError:
            self._audit_denied(
                actor_type=actor_type,
                actor_id=actor_id,
                action="job.retry",
                target_id=job_id,
            )
            raise

        self.repository(JobRepository).retry(job_id)

        self._audit(
            actor_type=actor_type,
            actor_id=actor_id,
            action="job.retry",
            target_id=job_id,
            context={"status": "queued"},
        )

        return self._require_job(job_id)
    # ── 6. increment_retry ────────────────────────────────
    def increment_retry(
        self,
        job_id: str,
        *,
        actor_id: str,
        actor_type: str = "agent",
    ) -> dict:
        if not _is_uuid(job_id):
            raise ValidationError("job_id must be a valid UUID.")
        if actor_type not in _VALID_ACTOR_TYPES:
            raise ValidationError(
                f"actor_type must be one of {_VALID_ACTOR_TYPES}."
            )
        self._require_job(job_id)
        # Authorization occurs inside the transaction, before the mutation.
        try:
            self._require_write(actor_id)
        except AuthorizationError:
            self._audit_denied(
                actor_type=actor_type,
                actor_id=actor_id,
                action="job.increment_retry",
                target_id=job_id,
            )
            raise
        # Repository performs retry_count = retry_count + 1 atomically.
        # The service does NOT recompute the counter.
        self.repository(JobRepository).increment_retry(job_id)
        self._audit(
            actor_type=actor_type,
            actor_id=actor_id,
            action="job.increment_retry",
            target_id=job_id,
            context={"status": "queued"},
        )
        return self._require_job(job_id)

    # ── 7. list_by_task ───────────────────────────────────
    def list_by_task(
        self, task_id: str, *, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        if not _is_uuid(task_id):
            raise ValidationError("task_id must be a valid UUID.")
        self._validate_paging(limit, offset)
        return self.repository(JobRepository).list_by_task(
            task_id, limit=limit, offset=offset
        )

    def list_by_agent(
        self, agent_id: str, *, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        if not _is_uuid(agent_id):
            raise ValidationError("agent_id must be a valid UUID.")

        self._validate_paging(limit, offset)

        return self.repository(JobRepository).list_by_agent(
            agent_id,
            limit=limit,
            offset=offset,
        )
    # ── 8. list_by_status ─────────────────────────────────
    def list_by_status(
        self, status: str, *, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        if status not in _VALID_STATUSES:
            raise ValidationError(
                f"status must be one of {_VALID_STATUSES}; got {status!r}."
            )
        self._validate_paging(limit, offset)
        return self.repository(JobRepository).list_by_status(
            status, limit=limit, offset=offset
        )