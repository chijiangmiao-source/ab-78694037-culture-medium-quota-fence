"""Configuration loaded exclusively from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    db_min_pool_size: int
    db_max_pool_size: int

    @staticmethod
    def from_env() -> "Settings":
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError(
                "DATABASE_URL is required, e.g. "
                "postgresql+psycopg://user:pass@host:5432/dbname"
            )
        return Settings(
            database_url=url,
            db_min_pool_size=int(os.environ.get("DB_MIN_POOL_SIZE", "2")),
            db_max_pool_size=int(os.environ.get("DB_MAX_POOL_SIZE", "16")),
        )


settings = Settings.from_env()
