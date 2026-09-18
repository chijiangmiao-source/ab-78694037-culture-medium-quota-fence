from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class BatchCreate(BaseModel):
    total_ml: int = Field(gt=0, description="batch capacity in integer millilitres; immutable after creation")


class BatchView(BaseModel):
    id: int
    total_ml: int
    available_ml: int
    reserved_ml: int
    confirmed_ml: int
    created_at: datetime


class ReserveCreate(BaseModel):
    amount_ml: int = Field(gt=0)
    lease_seconds: int = Field(ge=5, le=300)


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
