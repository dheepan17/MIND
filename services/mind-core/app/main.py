"""FastAPI application entrypoint for mind-core (Phase 5.1).

Establishes the minimal HTTP boundary:

* a FastAPI app via the :func:`create_app` factory (also exported as ``app``
  so existing imports keep working),
* service-error -> HTTP mapping (see :mod:`app.api.errors`),
* dependency-injected DB connection / transaction (see
  :mod:`app.api.dependencies`),
* a static ``/health`` endpoint that requires no database,
* the Agent API (``/agents``) as a thin transport adapter over
  :class:`app.services.agent_service.AgentService` (Phase 5.2).
"""
from __future__ import annotations

from fastapi import FastAPI

from app.api.errors import register_error_handlers
from app.api.routers import agents as agents_router
from app.api.routers import events as events_router
from app.api.routers import jobs as jobs_router
from app.api.routers import memories as memories_router
from app.api.routers import permissions as permissions_router
from app.api.routers import tasks as tasks_router
from app.api.routers import tools as tools_router

def create_app() -> FastAPI:
    """Build the mind-core FastAPI application."""
    application = FastAPI(
        title="M.I.N.D.",
        description="Multimodal Intelligence & Neural Diagnostics",
        version="0.1.0",
    )
    register_error_handlers(application)
    # Domain boundary: the Agent API is a thin transport adapter over
    # AgentService. Transaction ownership stays in the request dependency.
    application.include_router(agents_router.router)
    # Phase 5.3: the Task API is a thin transport adapter over TaskService,
    # following the same boundary and transaction-ownership conventions.
    application.include_router(tasks_router.router)
    # Phase 5.4: the Job API is a thin transport adapter over JobService,
    # following the same boundary and transaction-ownership conventions.
    application.include_router(jobs_router.router)
    # Phase 5.5: the Event API is a thin transport adapter over EventService,
    # following the same boundary and transaction-ownership conventions.
    # Append-only: only POST (create) and GET (read) are exposed.
    application.include_router(events_router.router)
    # Phase 5.6: the Memory API is a thin transport adapter over
    # MemoryService, following the same boundary and transaction-ownership
    # conventions. Secret-classification operations are blocked at the policy
    # boundary (no authentication layer exists yet).
    application.include_router(memories_router.router)
    # Phase 5.8: the Permissions API is a thin transport adapter over
    # PermissionService, following the same boundary and transaction-ownership
    # conventions. Every endpoint is gated on the ``admin:roles`` capability.
    application.include_router(permissions_router.router)
    # Phase 5.9: the Tool API is a thin transport adapter over ToolService,
    # following the same boundary and transaction-ownership conventions.
    # Mutations require the generic ``write`` capability.
    application.include_router(tools_router.router)

    @application.get("/")
    def root():
        return {"system": "M.I.N.D.", "call_name": "WEB", "status": "online"}

    @application.get("/health")
    def health():
        # Static liveness check: no DB required, so the endpoint works even
        # when DATABASE_URL is unset. A DB-aware probe can be added later
        # behind the existing connection infrastructure if needed.
        return {"system": "M.I.N.D.", "call_name": "WEB", "status": "online"}

    return application


app = create_app()