"""
Structured logging using structlog.

Logs are emitted as JSON in production (for log aggregators) and as pretty
colored console output in development. Import `get_logger()` anywhere:

    from app.core.logging import get_logger
    log = get_logger(__name__)
    log.info("invoice_created", invoice_id=str(inv.id), amount=inv.amount)
"""
from __future__ import annotations

import logging
import sys

import structlog

from app.config import settings


def configure_logging() -> None:
    """
    Configure structlog + stdlib logging. Idempotent — safe to call multiple times.
    Called once at app startup (see main.py) and by Celery workers.
    """
    log_level = getattr(logging, settings.log_level, logging.INFO)

    # Route stdlib logging (uvicorn, sqlalchemy, celery, etc.) through structlog.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
    )

    # Shared processors run before every log call.
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if settings.app_env == "development":
        # Human-friendly, colored output for dev.
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(colors=True),
        ]
    else:
        # JSON output for production log aggregation.
        processors = shared_processors + [
            structlog.processors.JSONRenderer(),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Quiet down noisy libraries in dev.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.database_echo else logging.WARNING
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger. Pass __name__ from the calling module."""
    return structlog.get_logger(name)
