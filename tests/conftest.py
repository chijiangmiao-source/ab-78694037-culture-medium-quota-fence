"""Test fixtures backed by a real PostgreSQL server.

A fresh ephemeral database is created per test session from the admin DSN
in ``TEST_DATABASE_URL`` (default postgresql://postgres@localhost:5433/
postgres). Migrations are applied exactly as in production.
"""
from __future__ import annotations

import os
import socket
import time
import uuid
from pathlib import Path

import httpx
import psycopg
import pytest
import uvicorn

ADMIN_DSN = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres@localhost:5433/postgres",
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def admin_conn():
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        yield conn


@pytest.fixture(scope="session")
def database(admin_conn):
    name = f"quota_test_{uuid.uuid4().hex[:12]}"
    admin_conn.execute(f'CREATE DATABASE "{name}"')
    # Derive the test DSN from the admin DSN, swapping only the database.
    parts = psycopg.conninfo.conninfo_to_dict(ADMIN_DSN)
    parts["dbname"] = name
    dsn = psycopg.conninfo.make_conninfo(**parts)
    os.environ["DATABASE_URL"] = dsn
    os.environ.setdefault("DB_MAX_POOL_SIZE", "32")
    os.environ.setdefault("DB_MIN_POOL_SIZE", "4")
    # Import after env is set so the pool binds to the ephemeral database.
    from app.db import get_pool, run_migrations

    run_migrations()
    yield dsn
    from app.db import close_pool

    close_pool()
    admin_conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture()
def pool(database):
    from app.db import get_pool

    p = get_pool()
    with p.connection() as conn:
        conn.execute("TRUNCATE TABLE batch RESTART IDENTITY CASCADE")
        conn.commit()
    yield p


@pytest.fixture()
def admin(database):
    """Autocommit connection for direct assertions and clock manipulation."""
    with psycopg.connect(database, autocommit=True) as conn:
        yield conn


@pytest.fixture()
def client(pool):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def live_server(pool):
    """Real uvicorn server in a daemon thread for end-to-end concurrency."""
    from app.main import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="warning")
    server = uvicorn.Server(config)
    import threading

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(base + "/health", timeout=1).status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.1)
    else:
        raise RuntimeError("live server never became healthy")
    yield base
    server.should_exit = True
    thread.join(timeout=10)


def assert_conserved(cur, batch_id: int) -> dict:
    total, available, reserved, confirmed = cur.execute(
        "SELECT total_ml, available_ml, reserved_ml, confirmed_ml "
        "FROM batch WHERE id = %s",
        (batch_id,),
    ).fetchone()
    assert total == available + reserved + confirmed, (
        f"conservation broken: total={total} available={available} "
        f"reserved={reserved} confirmed={confirmed}"
    )
    held_sum, confirmed_sum, finished_sum = cur.execute(
        """
        SELECT COALESCE(SUM(amount_ml) FILTER (WHERE status='held'), 0),
               COALESCE(SUM(amount_ml) FILTER (WHERE status='confirmed'), 0),
               COALESCE(SUM(amount_ml) FILTER
                        (WHERE status IN ('cancelled','expired')), 0)
          FROM reservation WHERE batch_id = %s
        """,
        (batch_id,),
    ).fetchone()
    assert held_sum == reserved, (held_sum, reserved)
    assert confirmed_sum == confirmed, (confirmed_sum, confirmed)
    assert total == available + held_sum + confirmed_sum
    return {"total": total, "available": available, "reserved": reserved,
            "confirmed": confirmed, "finished_sum": finished_sum}
