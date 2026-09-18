"""Provision a real, throwaway PostgreSQL cluster for the test session.

No mocks, no SQLite: every test talks to a genuine postgres backend. Point at
an existing toolchain with PGBIN=/path/to/pg/bin (must contain initdb/pg_ctl).
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app.db import init_schema
from app.main import app

PG_PORT_LOCK = socket.socket()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _find_pgbin() -> str | None:
    candidates = []
    if os.environ.get("PGBIN"):
        candidates.append(os.environ["PGBIN"])
    candidates += [
        "/tmp/pgenv/bin",
        "/usr/lib/postgresql/16/bin",
        "/usr/lib/postgresql/15/bin",
        "/usr/lib/postgresql/17/bin",
        "/usr/local/pgsql/bin",
    ]
    for c in candidates:
        if c and Path(c, "initdb").exists() and Path(c, "pg_ctl").exists():
            return c
    return None


@pytest.fixture(scope="session")
def pgbin() -> str:
    pgb = _find_pgbin()
    if pgb is None:
        pytest.skip("PostgreSQL not found; install it or set PGBIN")
    return pgb


@pytest.fixture(scope="session")
def pg_dsn(pgbin, tmp_path_factory):
    work = tmp_path_factory.mktemp("pgcluster")
    datadir = work / "data"
    sockdir = work / "sock"
    sockdir.mkdir()
    port = _free_port()

    env = dict(os.environ)
    init = subprocess.run(
        [
            f"{pgbin}/initdb",
            "-D", str(datadir),
            "-U", "postgres",
            "--auth=trust",
            "--encoding=UTF8",
            "--no-locale",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    if init.returncode != 0:
        raise RuntimeError(f"initdb failed:\n{init.stderr}\n{init.stdout}")

    logfile = work / "pg.log"
    start = subprocess.run(
        [
            f"{pgbin}/pg_ctl",
            "-D", str(datadir),
            "-l", str(logfile),
            "-w",
            "-o", (
                f"-p {port} -k {sockdir} -c fsync=off "
                "-c synchronous_commit=off -c full_page_writes=off "
                "-c max_connections=200"
            ),
            "start",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    if start.returncode != 0:
        raise RuntimeError(f"pg_ctl start failed:\n{start.stderr}\n{logfile.read_text()}")

    dsn = f"postgresql://postgres@127.0.0.1:{port}/postgres"
    try:
        yield dsn
    finally:
        subprocess.run(
            [f"{pgbin}/pg_ctl", "-D", str(datadir), "-m", "immediate", "-w", "stop"],
            env=env,
            capture_output=True,
        )
        shutil.rmtree(datadir, ignore_errors=True)


@pytest.fixture()
def pool(pg_dsn):
    from psycopg_pool import ConnectionPool

    p = ConnectionPool(
        pg_dsn,
        min_size=2,
        max_size=80,
        timeout=30,
        open=True,
        kwargs={"row_factory": dict_row},
    )
    init_schema(p)
    try:
        yield p
    finally:
        # Tables are created in the throwaway cluster; truncate between tests.
        with p.connection() as conn:
            conn.execute(
                "TRUNCATE reservations, batches RESTART IDENTITY RESTRICT"
            )
            conn.commit()
        p.close()


@pytest.fixture()
def client(pool, pg_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", pg_dsn)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def raw_conn(pg_dsn):
    """A connection outside the app pool, for clock freezing and assertions."""
    conn = psycopg.connect(pg_dsn, row_factory=dict_row, autocommit=False)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def fetch_batch(pool, batch_id: int) -> dict:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT * FROM batches WHERE id = %s", [batch_id]
        ).fetchone()
        return dict(row)


def assert_conserved(pool, batch_id: int) -> dict:
    b = fetch_batch(pool, batch_id)
    assert b["total_ml"] == b["available_ml"] + b["reserved_ml"] + b["confirmed_ml"], b
    assert b["available_ml"] >= 0 and b["reserved_ml"] >= 0 and b["confirmed_ml"] >= 0
    return b


def freeze_at(conn, ts) -> None:
    """Freeze the repository clock for one transaction (test-only GUC)."""
    conn.execute("SELECT set_config('app.frozen_now', %s, true)", [ts.isoformat()])
