"""Public database access API for mind-core (Phase 1).

Exposes only the connection and transaction primitives. Repositories and
services are intentionally out of scope for this phase.
"""
from __future__ import annotations

from app.db.connection import (
    DatabaseConfigError,
    DatabaseError,
    connect,
)
from app.db.transaction import transaction

__all__ = [
    "DatabaseConfigError",
    "DatabaseError",
    "connect",
    "transaction",
]