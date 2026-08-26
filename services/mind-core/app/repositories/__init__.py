"""Application persistence repositories for mind-core (Phase 3).

Thin, explicit-SQL adapters over the existing psycopg connection. They accept
an existing connection, never commit/rollback/close it, and contain no
business logic, authorization, or LLM reasoning.
"""
from __future__ import annotations

from app.repositories.agents import AgentRepository
from app.repositories.audit import AuditRepository
from app.repositories.base import BaseRepository
from app.repositories.events import EventRepository
from app.repositories.jobs import JobRepository
from app.repositories.memories import MemoryRepository
from app.repositories.permissions import PermissionRepository
from app.repositories.tasks import TaskRepository
from app.repositories.tools import ToolRepository

__all__ = [
    "AgentRepository",
    "AuditRepository",
    "BaseRepository",
    "EventRepository",
    "JobRepository",
    "MemoryRepository",
    "PermissionRepository",
    "TaskRepository",
    "ToolRepository",
]