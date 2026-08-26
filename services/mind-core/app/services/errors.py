"""Typed exceptions for the mind-core service layer (Phase 4.1).

A small, explicit hierarchy. Every service-layer error inherits from
:class:`ServiceError` so callers can catch the whole family with one
``except``. Concrete services wrap lower-level errors (e.g. database or
repository exceptions) and attach the original as the ``__cause__`` so
the chain is preserved.
"""
from __future__ import annotations


class ServiceError(Exception):
    """Base class for all service-layer errors.

    Inherits directly from :class:`Exception`. Concrete service errors
    (validation, not-found, authorization, policy) subclass this so a
    single ``except ServiceError`` covers the whole family.
    """


class ValidationError(ServiceError):
    """Raised when input fails service-layer validation.

    Covers malformed arguments, missing required fields, and values that
    violate invariants the service is responsible for (distinct from
    database-level constraint violations, which should be wrapped here).
    """


class NotFoundError(ServiceError):
    """Raised when a requested entity does not exist.

    Used instead of returning ``None`` when the caller needs a clear,
    typed signal that the target resource was not found.
    """


class AuthorizationError(ServiceError):
    """Raised when an actor is not permitted to perform an action.

    Distinct from :class:`PolicyViolationError`: this is about *who* may
    act (identity/role/capability), not whether the action itself is
    allowed by standing policy.
    """


class PolicyViolationError(ServiceError):
    """Raised when an action would violate a standing policy.

    Distinct from :class:`AuthorizationError`: the actor may be
    authenticated and have a capability, but the specific operation is
    forbidden by policy (e.g. destructive tool on a protected target).
    """