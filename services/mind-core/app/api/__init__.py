"""HTTP API boundary for mind-core (Phase 5.1).

This package holds transport-layer concerns only: dependency injection,
error mapping, and (later) routers. It contains no SQL, no business rules,
and no authorization logic. Persistence stays in repositories; domain rules
stay in services.
"""
from __future__ import annotations