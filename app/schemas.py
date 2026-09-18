"""Pydantic request/response schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BatchCreate(BaseModel):
    total_ml: int = Field(
        ..., gt=0, description="Immutable batch capacity in whole millilitres"
    )


class BatchView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    total_ml: int
    available_ml: int
    reserved_ml: int
    confirmed_ml: int
    created_at: datetime


class ReserveRequest(BaseModel):
    amount_ml: int = Field(..., gt=0)
    lease_seconds: int = Field(..., ge=5, le=300)


class BatchNested(BaseModel):
    id: int
    total_ml: int
    available_ml: int
    reserved_ml: int
    confirmed_ml: int
    created_at: datetime


class ReservationView(BaseModel):
    fence_token: int
    batch_id: int
    amount_ml: int
    status: Literal["held", "confirmed", "cancelled", "expired"]
    created_at: datetime
    expires_at: datetime
    confirmed_at: datetime | None
    cancelled_at: datetime | None
    batch: BatchNested | None = None
