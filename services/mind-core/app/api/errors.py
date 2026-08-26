"""HTTP error mapping for the mind-core API boundary (Phase 5.1).

Maps the service-layer exception hierarchy to HTTP responses. Only the
concrete domain errors are mapped; unexpected exceptions fall through to
FastAPI's default 500 handler so they remain distinguishable from domain
errors and never leak database internals, SQL, or stack traces to clients.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.security import AuthenticationError
from app.services.errors import (
    AuthorizationError,
    NotFoundError,
    PolicyViolationError,
    ValidationError,
)


def _handler(status_code: int, code: str):
    def _handle(request: Request, exc: Exception) -> JSONResponse:
        # ``str(exc)`` is a domain message (e.g. "owner is required"), never
        # a SQL statement or connection string.
        return JSONResponse(
            status_code=status_code,
            content={"error": code, "detail": str(exc)},
        )

    return _handle


def register_error_handlers(app: FastAPI) -> None:
    """Attach service-error -> HTTP exception handlers to ``app``."""
    app.add_exception_handler(ValidationError, _handler(400, "validation_error"))
    app.add_exception_handler(NotFoundError, _handler(404, "not_found"))
    app.add_exception_handler(AuthorizationError, _handler(403, "forbidden"))
    app.add_exception_handler(PolicyViolationError, _handler(403, "policy_violation"))
    app.add_exception_handler(AuthenticationError, _handler(401, "unauthorized"))