"""Service-layer foundation for mind-core (Phase 4.1 + 4.2 + 4.3 + 4.4).

Exposes the base service class, the typed exception hierarchy, and the
concrete business-logic services implemented so far (``PermissionService``,
``AuditService``, ``AgentService``). Later phases add the remaining
services and are intentionally not part of this module yet.
"""
from __future__ import annotations

from app.services.agent_service import AgentService
from app.services.audit_service import AuditService
from app.services.base import BaseService
from app.services.errors import (
    AuthorizationError,
    NotFoundError,
    PolicyViolationError,
    ServiceError,
    ValidationError,
)
from app.services.event_service import EventService
from app.services.job_service import JobService
from app.services.memory_service import MemoryService
from app.services.permission_service import PermissionService
from app.services.task_service import TaskService
from app.services.tool_service import ToolService

__all__ = [
    "AgentService",
    "AuditService",
    "AuthorizationError",
    "BaseService",
    "EventService",
    "JobService",
    "MemoryService",
    "NotFoundError",
    "PermissionService",
    "PolicyViolationError",
    "ServiceError",
    "TaskService",
    "ToolService",
    "ValidationError",
]
