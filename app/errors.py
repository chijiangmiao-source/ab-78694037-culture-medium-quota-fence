"""Structured error types shared by the service and HTTP layers."""
from __future__ import annotations


class ServiceError(Exception):
    """Base class for all expected, client-facing service errors."""

    error_code = "internal_error"
    http_status = 500

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_body(self) -> dict:
        return {"error": {"code": self.error_code, "message": self.message,
                          "details": self.details}}


class BatchNotFound(ServiceError):
    error_code = "batch_not_found"
    http_status = 404


class ReservationNotFound(ServiceError):
    error_code = "reservation_not_found"
    http_status = 404


class InvalidTotal(ServiceError):
    error_code = "invalid_total"
    http_status = 422


class InvalidAmount(ServiceError):
    error_code = "invalid_amount"
    http_status = 422


class InvalidLeaseDuration(ServiceError):
    error_code = "invalid_lease_duration"
    http_status = 422


class InsufficientQuota(ServiceError):
    error_code = "insufficient_quota"
    http_status = 409


class ReservationExpired(ServiceError):
    error_code = "reservation_expired"
    http_status = 409


class ReservationCancelled(ServiceError):
    error_code = "reservation_cancelled"
    http_status = 409


class ReservationAlreadyConfirmed(ServiceError):
    """Idempotent success is returned by the service, never raised."""

    error_code = "reservation_already_confirmed"
    http_status = 409
