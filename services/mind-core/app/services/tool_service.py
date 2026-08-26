"""Tool registry service (Phase 3).

Thin orchestration over :class:`ToolRepository` and
:class:`PermissionRepository`. It validates inputs, enforces the
explicit-allow contract (a tool is denied unless a capability grants it),
and records audit entries for mutating operations. It contains no
authorization decisions of its own and does not invent capabilities.
"""
from __future__ import annotations

import uuid

from app.repositories.audit import AuditRepository
from app.repositories.permissions import PermissionRepository
from app.repositories.tools import ToolRepository
from app.services.base import BaseService
from app.services.errors import (
    NotFoundError,
    ValidationError,
)

_VALID_STATUS = {"active", "disabled"}
_MAX_NAME = 255


class ToolService(BaseService):
    """Manage the tool registry and its capability linkage."""

    def __init__(
        self,
        tools: ToolRepository,
        permissions: PermissionRepository,
        audit: AuditRepository,
    ) -> None:
        self._tools = tools
        self._permissions = permissions
        self._audit = audit

    # ── helpers ────────────────────────────────────────────
    @staticmethod
    def _validate_uuid(value: str, field: str) -> str:
        try:
            uuid.UUID(str(value))
        except (ValueError, AttributeError, TypeError):
            raise ValidationError(f"{field} must be a valid UUID")
        return str(value)

    @staticmethod
    def _validate_paging(limit: int, offset: int) -> tuple[int, int]:
        if not isinstance(limit, int) or limit < 1 or limit > 100:
            raise ValidationError("limit must be an integer between 1 and 100")
        if not isinstance(offset, int) or offset < 0:
            raise ValidationError("offset must be a non-negative integer")
        return limit, offset

    def _validate_capability_key(self, key: str | None) -> None:
        # requires_capability references capabilities(key). The FK enforces
        # referential integrity at the DB layer; we additionally validate at
        # the service boundary so callers get a clear error before insert.
        if key is None:
            return
        if not isinstance(key, str) or not key.strip():
            raise ValidationError("requires_capability must be a non-empty string")
        if self._permissions.get_capability_by_key(key) is None:
            raise ValidationError(
                f"requires_capability '{key}' does not match any capability"
            )

    # ── create ─────────────────────────────────────────────
    def create_tool(
        self,
        name: str,
        description: str | None = None,
        version: str | None = None,
        requires_capability: str | None = None,
        is_destructive: bool = False,
        is_verified: bool = False,
        config: dict | None = None,
        status: str = "active",
        actor_type: str = "agent",
        actor_id: str = "system",
    ) -> dict:
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("name must be a non-empty string")
        if len(name) > _MAX_NAME:
            raise ValidationError(f"name must be <= {_MAX_NAME} characters")
        if status not in _VALID_STATUS:
            raise ValidationError(
                f"status must be one of {sorted(_VALID_STATUS)}"
            )
        if not isinstance(is_destructive, bool):
            raise ValidationError("is_destructive must be a boolean")
        if not isinstance(is_verified, bool):
            raise ValidationError("is_verified must be a boolean")
        if config is not None and not isinstance(config, dict):
            raise ValidationError("config must be a dict or None")

        self._validate_capability_key(requires_capability)

        tool = self._tools.create(
            name=name,
            description=description,
            version=version,
            requires_capability=requires_capability,
            is_destructive=is_destructive,
            is_verified=is_verified,
            config=config,
            status=status,
        )

        self._audit.append(
            actor_type=actor_type,
            actor_id=actor_id,
            action="tool.create",
            outcome="success",
            target_type="tool",
            target_id=str(tool["id"]),
            capability_used=None,
            context={
                "name": name,
                "requires_capability": requires_capability,
                "is_destructive": is_destructive,
            },
        )
        return tool

    # ── read ───────────────────────────────────────────────
    def get_tool(self, tool_id: str) -> dict:
        self._validate_uuid(tool_id, "tool_id")
        tool = self._tools.get(tool_id)
        if tool is None:
            raise NotFoundError(f"tool '{tool_id}' not found")
        return tool

    def get_tool_by_name(self, name: str) -> dict:
        if not isinstance(name, str) or not name.strip():
            raise ValidationError("name must be a non-empty string")
        tool = self._tools.get_by_name(name)
        if tool is None:
            raise NotFoundError(f"tool '{name}' not found")
        return tool

    def list_destructive(self, limit: int = 50, offset: int = 0) -> list[dict]:
        limit, offset = self._validate_paging(limit, offset)
        return self._tools.list_destructive(limit=limit, offset=offset)

    def list_by_capability(
        self, capability_key: str, limit: int = 50, offset: int = 0
    ) -> list[dict]:
        if not isinstance(capability_key, str) or not capability_key.strip():
            raise ValidationError("capability_key must be a non-empty string")
        limit, offset = self._validate_paging(limit, offset)
        return self._tools.list_by_capability(
            capability_key, limit=limit, offset=offset
        )

    # ── update ─────────────────────────────────────────────
    def update_status(
        self,
        tool_id: str,
        status: str,
        actor_type: str = "agent",
        actor_id: str = "system",
    ) -> None:
        self._validate_uuid(tool_id, "tool_id")
        if status not in _VALID_STATUS:
            raise ValidationError(
                f"status must be one of {sorted(_VALID_STATUS)}"
            )
        if self._tools.get(tool_id) is None:
            raise NotFoundError(f"tool '{tool_id}' not found")
        self._tools.update_status(tool_id, status)
        self._audit.append(
            actor_type=actor_type,
            actor_id=actor_id,
            action="tool.update_status",
            outcome="success",
            target_type="tool",
            target_id=tool_id,
            capability_used=None,
            context={"status": status},
        )

    def mark_verified(
        self,
        tool_id: str,
        is_verified: bool = True,
        actor_type: str = "agent",
        actor_id: str = "system",
    ) -> None:
        self._validate_uuid(tool_id, "tool_id")
        if not isinstance(is_verified, bool):
            raise ValidationError("is_verified must be a boolean")
        if self._tools.get(tool_id) is None:
            raise NotFoundError(f"tool '{tool_id}' not found")
        self._tools.mark_verified(tool_id, is_verified)
        self._audit.append(
            actor_type=actor_type,
            actor_id=actor_id,
            action="tool.mark_verified",
            outcome="success",
            target_type="tool",
            target_id=tool_id,
            capability_used=None,
            context={"is_verified": is_verified},
        )
