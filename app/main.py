from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Iterator

from fastapi import Depends, FastAPI, status
from psycopg import Connection

from . import repository
from .db import create_pool, init_schema
from .errors import register_exception_handlers
from .models import (
    BatchCreate,
    BatchView,
    HealthView,
    ReserveCreate,
    ReservationView,
)

POOL_MAX_SIZE = int(os.environ.get("DB_POOL_MAX_SIZE", "20"))


def get_conn() -> Iterator[Connection]:
    # Commits on clean exit, rolls back when the handler raises.
    with app.state.pool.connection() as conn:
        yield conn


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = create_pool(max_size=POOL_MAX_SIZE)
    init_schema(app.state.pool)
    try:
        yield
    finally:
        app.state.pool.close()


app = FastAPI(
    title="Medium Reservation Fence Service",
    version="1.0.0",
    lifespan=lifespan,
)
register_exception_handlers(app)


@app.get("/health", response_model=HealthView, tags=["health"])
def health(conn: Connection = Depends(get_conn)) -> HealthView:
    conn.execute("SELECT 1")
    return HealthView(status="ok", database="ok")


@app.post("/batches", response_model=BatchView, status_code=status.HTTP_201_CREATED, tags=["batches"])
def create_batch(body: BatchCreate, conn: Connection = Depends(get_conn)) -> BatchView:
    return repository.create_batch(conn, body.total_ml)


@app.get("/batches", response_model=list[BatchView], tags=["batches"])
def list_batches(conn: Connection = Depends(get_conn)) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, total_ml, available_ml, reserved_ml, confirmed_ml, created_at
          FROM batches
         ORDER BY id
        """
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/batches/{batch_id}", response_model=BatchView, tags=["batches"])
def get_batch(batch_id: int, conn: Connection = Depends(get_conn)) -> BatchView:
    return repository.get_batch(conn, batch_id)


@app.post(
    "/batches/{batch_id}/reservations",
    response_model=ReservationView,
    status_code=status.HTTP_201_CREATED,
    tags=["reservations"],
)
def reserve(batch_id: int, body: ReserveCreate, conn: Connection = Depends(get_conn)) -> ReservationView:
    return repository.reserve(conn, batch_id, body.amount_ml, body.lease_seconds)


@app.get(
    "/batches/{batch_id}/reservations/{token}",
    response_model=ReservationView,
    tags=["reservations"],
)
def get_reservation(batch_id: int, token: int, conn: Connection = Depends(get_conn)) -> ReservationView:
    return repository.get_reservation(conn, batch_id, token)


@app.post(
    "/batches/{batch_id}/reservations/{token}/confirm",
    response_model=ReservationView,
    tags=["reservations"],
)
def confirm(batch_id: int, token: int, conn: Connection = Depends(get_conn)) -> ReservationView:
    return repository.confirm(conn, batch_id, token)


@app.post(
    "/batches/{batch_id}/reservations/{token}/cancel",
    response_model=ReservationView,
    tags=["reservations"],
)
def cancel(batch_id: int, token: int, conn: Connection = Depends(get_conn)) -> ReservationView:
    return repository.cancel(conn, batch_id, token)
