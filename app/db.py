"""Database connection pool and migration bootstrap."""
from __future__ import annotations

from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_pool: ConnectionPool | None = None


def _libpq_dsn(url: str) -> str:
    # Accept both plain libpq URIs and SQLAlchemy-style URLs.
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            _libpq_dsn(settings.database_url),
            min_size=settings.db_min_pool_size,
            max_size=settings.db_max_pool_size,
            kwargs={"autocommit": False, "row_factory": dict_row},
            open=True,
        )
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def run_migrations() -> None:
    """Apply every pending migration file inside one transaction each."""
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    with get_pool().connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        applied = {r["version"] for r in conn.execute(
            "SELECT version FROM schema_migrations"
        ).fetchall()}
        for path in files:
            version = path.stem
            if version in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            with conn.transaction():
                conn.execute(sql)
        conn.commit()
