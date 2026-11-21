"""Domain errors with HTTP status codes and optional row-level details."""

from __future__ import annotations


class LedgerError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, details: list[dict] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or []


class ValidationError(LedgerError):
    status_code = 422
    code = "invalid_input"


class ConflictError(LedgerError):
    status_code = 409
    code = "conflict"


class BatchRejected(ConflictError):
    code = "batch_rejected"


class NotFoundError(LedgerError):
    status_code = 404
    code = "not_found"


class NotInitializedError(ConflictError):
    code = "not_initialized"


class AlreadyInitializedError(ConflictError):
    code = "already_initialized"


class BusyError(LedgerError):
    status_code = 503
    code = "busy"
