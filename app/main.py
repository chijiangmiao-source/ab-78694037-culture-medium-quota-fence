"""FastAPI application wiring and HTTP endpoint definitions."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import service
from .db import close_pool, get_pool, run_migrations
from .errors import ServiceError
from .schemas import (
    BatchCreate,
    BatchView,
    ReservationView,
    ReserveRequest,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_pool()
    run_migrations()
    yield
    close_pool()


app = FastAPI(
    title="Culture-medium Quota Service",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "Reserve/confirm/cancel whole-millilitre quota leases against "
        "immutable batch budgets. Fence tokens are strictly increasing "
        "within a batch and never reused. Every deadline uses the "
        "database clock."
    ),
)


@app.exception_handler(ServiceError)
async def service_error_handler(request: Request, exc: ServiceError):
    return JSONResponse(
        status_code=exc.http_status,
        content=jsonable_encoder(exc.to_body()),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request,
                                   exc: RequestValidationError):
    # Structured validation errors: no framework default shape leaks out.
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder({
            "error": {
                "code": "validation_error",
                "message": "request payload failed validation",
                "details": {"issues": exc.errors()},
            }
        }),
    )


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}


@app.post("/batches", response_model=BatchView, status_code=201,
          tags=["batches"])
def create_batch(payload: BatchCreate):
    return service.create_batch(get_pool(), payload.total_ml)


@app.get("/batches/{batch_id}", response_model=BatchView, tags=["batches"])
def get_batch(batch_id: int):
    return service.get_batch(get_pool(), batch_id)


@app.post("/batches/{batch_id}/reservations",
          response_model=ReservationView, status_code=201,
          tags=["reservations"])
def reserve_quota(batch_id: int, payload: ReserveRequest):
    return service.reserve_quota(
        get_pool(), batch_id, payload.amount_ml, payload.lease_seconds
    )


@app.post("/reservations/{fence_token}/confirm",
          response_model=ReservationView, tags=["reservations"])
def confirm_reservation(fence_token: int):
    return service.confirm_reservation(get_pool(), fence_token)


@app.post("/reservations/{fence_token}/cancel",
          response_model=ReservationView, tags=["reservations"])
def cancel_reservation(fence_token: int):
    return service.cancel_reservation(get_pool(), fence_token)


@app.get("/reservations/{fence_token}",
         response_model=ReservationView, tags=["reservations"])
def get_reservation(fence_token: int):
    return service.get_reservation(get_pool(), fence_token)
