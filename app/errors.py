from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger("medium")


class ErrorCode(str, Enum):
    VALIDATION_ERROR = "validation_error"
    BATCH_NOT_FOUND = "batch_not_found"
    RESERVATION_NOT_FOUND = "reservation_not_found"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    RESERVATION_EXPIRED = "reservation_expired"
    RESERVATION_CANCELLED = "reservation_cancelled"
    RESERVATION_CONFIRMED = "reservation_already_confirmed"
    TOTAL_IMMUTABLE = "total_immutable"
    INVALID_STATE_TRANSITION = "invalid_state_transition"
    INTERNAL_ERROR = "internal_error"


class ServiceError(Exception):
    """An error that is rendered as a structured envelope."""

    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}

    def to_response(self) -> JSONResponse:
        body: dict[str, Any] = {
            "error": {
                "code": self.code.value,
                "message": self.message,
            }
        }
        if self.details:
            body["error"]["details"] = self.details
        return JSONResponse(status_code=self.status_code, content=body)


def register_exception_handlers(app) -> None:
    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return exc.to_response()

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": ErrorCode.VALIDATION_ERROR.value,
                    "message": "request validation failed",
                    "details": {"issues": jsonable_encoder(exc.errors())},
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error: %s", exc)
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": ErrorCode.INTERNAL_ERROR.value,
                    "message": "internal server error",
                }
            },
        )
