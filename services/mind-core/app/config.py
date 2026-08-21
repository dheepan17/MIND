"""Application configuration for mind-core."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    name: str = "mind-core"
    environment: str = "development"
    log_level: str = "INFO"


def load_config() -> Config:
    """Load configuration from defaults.

    Environment variables / config files can be merged here later.
    """
    return Config()
