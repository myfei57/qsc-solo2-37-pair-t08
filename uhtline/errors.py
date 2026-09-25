"""Typed failures raised by the control, persistence and console layers."""

from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    """Base class carrying a stable error code and a structured detail payload."""

    code = "service-error"
    status = 400

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, "details": self.details}


class ValidationError(ServiceError):
    code = "validation-error"
    status = 422


class RangeError(ServiceError):
    """Raised when a command value sits outside the configured envelope."""

    code = "out-of-range"
    status = 422


class NotFoundError(ServiceError):
    code = "not-found"
    status = 404


class StateError(ServiceError):
    code = "state-error"
    status = 409


class InterlockError(ServiceError):
    code = "interlock-error"
    status = 409


class StageOrderError(ServiceError):
    code = "stage-order"
    status = 409


class GateClosedError(ServiceError):
    code = "gate-closed"
    status = 409


class LatchActiveError(ServiceError):
    code = "latch-active"
    status = 423


class StaleGenerationError(ServiceError):
    code = "stale-generation"
    status = 409


class StaleWarrantyError(ServiceError):
    code = "stale-warranty"
    status = 409


class DuplicateError(ServiceError):
    code = "duplicate"
    status = 409


class WatermarkError(ServiceError):
    code = "watermark"
    status = 409


class ConcurrencyConflictError(ServiceError):
    code = "concurrency-conflict"
    status = 409


class PersistenceError(ServiceError):
    code = "persistence-error"
    status = 500


__all__ = [
    "ConcurrencyConflictError",
    "DuplicateError",
    "GateClosedError",
    "InterlockError",
    "LatchActiveError",
    "NotFoundError",
    "PersistenceError",
    "RangeError",
    "ServiceError",
    "StageOrderError",
    "StaleGenerationError",
    "StaleWarrantyError",
    "StateError",
    "ValidationError",
    "WatermarkError",
]
