"""Identity boundary foundation for mind-core (Phase 5.7A).

Establishes the *trusted* security-context seam without implementing any
concrete authentication mechanism. Per the approved scope this module adds
NO JWT, OAuth, API keys, secrets store, middleware, or ``request.state``.

The central invariant this module enforces:

    caller-controlled actor_id / actor_type (HTTP request body)
        !=
    authenticated principal identity (SecurityContext)

A :class:`SecurityContext` is the ONLY trusted representation of "who is
calling". It is produced exclusively by a *resolver* (pluggable) or the
server-side :func:`internal_security_context` helper, never by parsing the
HTTP request. Routers must pass ``ctx.actor_id`` / ``ctx.actor_type`` into
services; they must never forward ``body.actor_id`` / ``body.actor_type``
on an authenticated route.

No schema, migration, capability, or authorization changes are made here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request

# An authenticated subject is always an ``agent`` or a ``human`` operator.
# ``role`` is an RBAC attribute of an agent, never an actor, and must never
# appear in a trusted SecurityContext (this prevents a self-asserted role
# identity from being treated as a principal).
_VALID_AUTH_ACTOR_TYPES = ("agent", "human")


class AuthenticationError(Exception):
    """Raised when an external request fails to authenticate.

    Mapped to HTTP 401 by :mod:`app.api.errors`. This is a transport/boundary
    concern, not a service-layer error, so it does not inherit
    :class:`app.services.errors.ServiceError`.
    """


@dataclass(frozen=True)
class SecurityContext:
    """Trusted representation of the authenticated caller.

    Never constructed from HTTP request data. For an ``agent`` or ``human``
    subject, ``actor_id`` must correspond to a real persisted identity
    (the installed resolver is responsible for that validation before
    construction). ``source`` records whether the context originated from an
    external credential exchange (``'external'``) or trusted server-side code
    (``'internal'``).
    """

    actor_type: str
    actor_id: str
    authenticated: bool
    source: str  # 'external' | 'internal'

    def __post_init__(self) -> None:
        if self.actor_type not in _VALID_AUTH_ACTOR_TYPES:
            raise ValueError(
                f"actor_type must be one of {_VALID_AUTH_ACTOR_TYPES}; got {self.actor_type!r}"
            )
        if not self.actor_id:
            raise ValueError("actor_id is required for a SecurityContext")
        if self.source not in ("external", "internal"):
            raise ValueError(f"source must be 'external' or 'internal'; got {self.source!r}")


# ── Resolver seam ──────────────────────────────────────────────
# A resolver turns an inbound HTTP request into a trusted SecurityContext, or
# raises AuthenticationError. Phase 5.7A ships NO external credential
# mechanism, so the default resolver denies every external request. A concrete
# mechanism (JWT / OAuth / API-key) plugs in later by calling
# :func:`set_security_context_resolver`.
_Resolver = Callable[[Request], "SecurityContext"]


def _default_resolver(request: Request) -> SecurityContext:
    # No authentication mechanism is configured in 5.7A. Every external HTTP
    # request is denied; trusted internal contexts are produced server-side
    # via internal_security_context(), never from request data. External
    # callers therefore can never obtain source='internal'.
    raise AuthenticationError("authentication is required but no mechanism is configured")


_resolver: _Resolver = _default_resolver


def set_security_context_resolver(resolver: _Resolver) -> None:
    """Install a pluggable principal resolver (tests / future mechanisms)."""
    global _resolver
    _resolver = resolver


def get_security_context_resolver() -> _Resolver:
    return _resolver


def get_security_context(request: Request) -> SecurityContext:
    """FastAPI dependency: resolve the trusted caller for this request.

    The resolver is the sole authority for identity and for ``source``. An
    external request can never produce ``source='internal'`` because only the
    resolver controls it, and the default resolver denies outright.
    """
    return _resolver(request)


def internal_security_context(actor_type: str, actor_id: str) -> SecurityContext:
    """Build a trusted INTERNAL security context (server-side only).

    Must never be reachable from request data. External requests cannot invoke
    this; only trusted in-process code may. ``actor_type`` of ``'role'`` is
    rejected by :class:`SecurityContext` validation.
    """
    return SecurityContext(
        actor_type=actor_type,
        actor_id=actor_id,
        authenticated=True,
        source="internal",
    )


def get_optional_security_context(request: Request) -> SecurityContext | None:
    """FastAPI dependency: resolve the trusted caller, or None if unauthenticated.

    Unlike :func:`get_security_context`, this does not raise on authentication
    failure. It returns ``None`` for unauthenticated requests, allowing public
    and internal memory reads to proceed without a trusted identity. Secret
    reads still require an authenticated context with ``data:pii``, enforced
    in the service layer.
    """
    try:
        return _resolver(request)
    except AuthenticationError:
        return None
