"""
Cross-cutting HTTP middleware and exception handlers.

Registered in `app.main.create_app()`. Responsible for:
    * CORS (so the Vite dev server on :5173 can call :8000)
    * Request/response logging with timing
    * Translating our custom AppException hierarchy into the standard
      error envelope defined in spec section 27.2.
    * Translating Pydantic validation errors into the same envelope.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.core.exceptions import AppException
from app.core.logging import get_logger

log = get_logger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    Attaches a request_id to every request, logs timing, and binds the
    request_id to structlog contextvars so all downstream logs carry it.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable],
    ):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            path=request.url.path,
            method=request.method,
        )

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - start) * 1000
            log.exception("request_failed", elapsed_ms=round(elapsed_ms, 2))
            raise

        elapsed_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Request-ID"] = request_id
        # Only log non-health endpoints to avoid noise from k8s liveness probes.
        if request.url.path not in {"/health", "/api/health"}:
            log.info(
                "request_completed",
                status_code=response.status_code,
                elapsed_ms=round(elapsed_ms, 2),
            )
        return response


def register_middleware(app: FastAPI) -> None:
    """Register all middleware on the FastAPI app."""

    # CORS — permissive in dev, restricted to allowed_origins_list in prod.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    # Request logging (added last, so it runs first — Starlette wraps in reverse order).
    app.add_middleware(RequestLoggingMiddleware)


def register_exception_handlers(app: FastAPI) -> None:
    """Translate exceptions into the standard error envelope."""

    @app.exception_handler(AppException)
    async def handle_app_exception(_: Request, exc: AppException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Convert Pydantic errors into our standard envelope.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Request validation failed.",
                    "details": {"errors": exc.errors()},
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        # Last-resort handler — never leak internals to the client.
        log.exception("unhandled_exception", exc_type=type(exc).__name__)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "An unexpected error occurred.",
                }
            },
        )
