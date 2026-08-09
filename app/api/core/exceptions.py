"""
Custom exception hierarchy.

Route handlers raise these; middleware translates them into the standard
error envelope defined in the spec (section 27.2):

    { "error": { "code": "...", "message": "...", "details": {...} } }
"""
from __future__ import annotations

from typing import Any


class AppException(Exception):
    """
    Base class for all application errors.

    Subclasses set `status_code` (HTTP status) and `code` (short error code
    string returned to the client).
    """

    status_code: int = 500
    code: str = "INTERNAL_ERROR"
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.details = details or {}
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to the standard error envelope shape."""
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return {"error": payload}


# ---- 400s ------------------------------------------------------------------
class BadRequestError(AppException):
    status_code = 400
    code = "BAD_REQUEST"
    message = "Invalid request."


class UnauthorizedError(AppException):
    status_code = 401
    code = "UNAUTHORIZED"
    message = "Authentication required."


class ForbiddenError(AppException):
    status_code = 403
    code = "FORBIDDEN"
    message = "You do not have permission to perform this action."


class NotFoundError(AppException):
    status_code = 404
    code = "NOT_FOUND"
    message = "Resource not found."


class ConflictError(AppException):
    status_code = 409
    code = "CONFLICT"
    message = "Resource already exists or state conflict."


class ValidationError(AppException):
    status_code = 422
    code = "VALIDATION_ERROR"
    message = "Validation failed."


class RateLimitError(AppException):
    status_code = 429
    code = "RATE_LIMITED"
    message = "Too many requests."


# ---- 500s ------------------------------------------------------------------
class ExternalServiceError(AppException):
    """A downstream service (Gmail, AI provider, etc.) failed."""

    status_code = 502
    code = "EXTERNAL_SERVICE_ERROR"
    message = "An external service failed."
