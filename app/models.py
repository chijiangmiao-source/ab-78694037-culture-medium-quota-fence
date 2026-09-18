from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field

# Amounts are integer millilitres: floats (even 3.0) and booleans are rejected
# at the boundary instead of being coerced.
PositiveMl = Annotated[int, Field(strict=True, gt=0)]
LeaseSeconds = Annotated[int, Field(strict=True, ge=5, le=300)]


class BatchCreate(BaseModel):
    total_ml: PositiveMl = Field(description="batch capacity in integer millilitres; immutable after creation")


class BatchView(BaseModel):
    id: int
    total_ml: int
    available_ml: int
    reserved_ml: int
    confirmed_ml: int
    created_at: datetime


class ReserveCreate(BaseModel):
    amount_ml: PositiveMl
    lease_seconds: LeaseSeconds


class ReservationView(BaseModel):
    batch_id: int
    token: int
    amount_ml: int
    status: str
    created_at: datetime
    expires_at: datetime
    confirmed_at: datetime | None = None
    cancelled_at: datetime | None = None
    settled_at: datetime | None = None


class HealthView(BaseModel):
    status: str
    database: str
