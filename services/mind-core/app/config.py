"""Application configuration for mind-core."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    name: str = "mind-core"
    environment: str = "development"
    log_level: str = "INFO"
    # Database connection URLs are sourced ONLY from the environment.
    # They are never hard-coded and never logged.
    database_url: str | None = None
    test_database_url: str | None = None


def load_config() -> Config:
    """Load configuration from environment variables.

    ``DATABASE_URL`` / ``TEST_DATABASE_URL`` are read from the environment
    only. No credentials are hard-coded and URLs are never logged.
    """
    return Config(
        database_url=os.environ.get("DATABASE_URL") or None,
        test_database_url=os.environ.get("TEST_DATABASE_URL") or None,
    )
