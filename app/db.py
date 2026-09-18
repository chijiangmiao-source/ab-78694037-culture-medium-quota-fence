from __future__ import annotations

import os
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

DEFAULT_DSN = "postgresql://medium:medium@127.0.0.1:5432/medium"


def create_pool(dsn: str | None = None, *, min_size: int = 2, max_size: int = 20) -> ConnectionPool:
    return ConnectionPool(
        dsn or os.environ.get("DATABASE_URL", DEFAULT_DSN),
        min_size=min_size,
        max_size=max_size,
        timeout=30,
        open=True,
        kwargs={"connect_timeout": 10, "row_factory": dict_row},
    )


def init_schema(pool: ConnectionPool) -> None:
    """Apply the idempotent DDL. Safe to run on every process start."""
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    with pool.connection() as conn:
        conn.execute(ddl)
        conn.commit()
