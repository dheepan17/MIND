"""AuditService — thin service-layer boundary around AuditRepository (Phase 4.3).

This is a low-level, append-only audit boundary. It records successful,
denied, and failed operations and exposes read/list access. It contains
no SQL, creates no connections, and never commits or rolls back — the
caller owns the transaction.

It deliberately does NOT depend on ``PermissionService`` (no circular
dependency, and audit must be able to record denied operations). All
persistence goes through :class:`AuditRepository`, which is append-only;
this service preserves that immutability by exposing no update/delete
methods.
"""
from __future__ import annotations

from app.repositories.audit import AuditRepository
from app.services.base import BaseService
from app.services.errors import ValidationError

_VALID_OUTCOMES = ("success", "denied", "error")
_MAX_LIMIT = 100


class AuditService(BaseService):
    """Append-only audit boundary over ``audit_logs``."""

    def record(
        self,
        *,
        actor_type: str,
        actor_id: str | None,
        action: str,
        outcome: str,
        target_type: str | None = None,
        target_id: str | None = None,
        capability_used: str | None = None,
        context: dict | None = None,
    ) -> dict:
        """Append an audit record. Returns the persisted row.

        Validation is performed at the service boundary even though the
        database also enforces constraints. ``outcome`` must be one of
        ``success`` / ``denied`` / ``error``; ``actor_type`` and ``action``
        must be non-empty; optional string fields must not be empty when
        provided.
        """
        if not isinstance(actor_type, str) or not actor_type:
            raise ValidationError("actor_type must be a non-empty string.")
        if not isinstance(action, str) or not action:
            raise ValidationError("action must be a non-empty string.")
        if outcome not in _VALID_OUTCOMES:
            raise ValidationError(
                f"outcome must be one of {_VALID_OUTCOMES}; got {outcome!r}."
            )
        if target_type is not None and not target_type:
            raise ValidationError("target_type must not be an empty string.")
        if capability_used is not None and not capability_used:
            raise ValidationError(
                "capability_used must not be an empty string."
            )

        # Delegate persistence; no SQL, no connection creation.
        return self.repository(AuditRepository).append(
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            outcome=outcome,
            target_type=target_type,
            target_id=target_id,
            capability_used=capability_used,
            context=context,
        )

    def get(self, audit_id) -> dict | None:
        """Return the audit record with ``audit_id``, or ``None``."""
        return self.repository(AuditRepository).get(audit_id)

    def list_by_actor(
        self,
        actor_type: str,
        actor_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """List audit records for a given actor, newest first."""
        self._validate_paging(limit, offset)
        return self.repository(AuditRepository).list_by_actor(
            actor_type, actor_id, limit=limit, offset=offset
        )

    def list_by_target(
        self,
        target_type: str,
        target_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """List audit records for a given target, newest first."""
        self._validate_paging(limit, offset)
        return self.repository(AuditRepository).list_by_target(
            target_type, target_id, limit=limit, offset=offset
        )

    def list_by_outcome(
        self,
        outcome: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """List audit records with a given outcome, newest first."""
        self._validate_paging(limit, offset)
        return self.repository(AuditRepository).list_by_outcome(
            outcome, limit=limit, offset=offset
        )

    @staticmethod
    def _validate_paging(limit: int, offset: int) -> None:
        if not isinstance(limit, int) or limit < 1 or limit > _MAX_LIMIT:
            raise ValidationError(
                f"limit must be an int between 1 and {_MAX_LIMIT}."
            )
        if not isinstance(offset, int) or offset < 0:
            raise ValidationError("offset must be an int >= 0.")