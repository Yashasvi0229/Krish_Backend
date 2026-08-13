"""
FastAPI application entry point.

Run locally:
    uvicorn app.main:app --reload --port 8000

On Render (see render.yaml):
    uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.config import settings
from app.core.logging import configure_logging, get_logger
from app.core.middleware import register_exception_handlers, register_middleware
from app.database import dispose_engine

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Runs on startup / shutdown. Configure logging, clean up connections."""
    configure_logging()
    log.info(
        "app_starting",
        env=settings.app_env,
        version=settings.app_version,
    )
    yield
    log.info("app_shutting_down")
    await dispose_engine()


def create_app() -> FastAPI:
    """Build and return the FastAPI app. Called once at import time below."""
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        lifespan=lifespan,
    )

    register_middleware(app)
    register_exception_handlers(app)

    # ---- Routers ----
    # Health is available at both `/health` (for Render's healthcheck)
    # and `/api/health` (for the frontend's /api proxy).
    app.include_router(health_router)
    app.include_router(health_router, prefix="/api")

    # Step 3 — auth + users + gmail connection
    from app.api.auth import router as auth_router
    from app.api.gmail import router as gmail_router
    from app.api.users import router as users_router

    app.include_router(auth_router,  prefix="/api/auth",  tags=["auth"])
    app.include_router(users_router, prefix="/api/users", tags=["users"])
    app.include_router(gmail_router, prefix="/api/gmail", tags=["gmail"])

    # Step 4 — jobs, emails, websockets
    from app.api.debug import router as debug_router
    from app.api.emails import router as emails_router
    from app.api.jobs import router as jobs_router
    from app.api.websockets import router as ws_router

    app.include_router(jobs_router,   prefix="/api/jobs", tags=["jobs"])
    app.include_router(emails_router, prefix="/api",      tags=["emails"])
    app.include_router(ws_router,     prefix="/api",      tags=["websockets"])
    app.include_router(debug_router,  prefix="/api/debug", tags=["debug"])

    # Step 5 — AI analysis, invoice draft + finalized invoice
    from app.api.analysis import router as analysis_router
    from app.api.invoices import router as invoices_router

    app.include_router(analysis_router, prefix="/api/claims", tags=["ai-analysis"])
    app.include_router(invoices_router, prefix="/api",        tags=["invoices"])

    # Phase 1 additions — dashboard stats + read-only rules/clients for the
    # review UI dropdowns and the admin listing pages.
    from app.api.dashboard import router as dashboard_router
    from app.api.rules import router as rules_router
    from app.api.clients import router as clients_router

    app.include_router(dashboard_router, prefix="/api/dashboard", tags=["dashboard"])
    app.include_router(rules_router,     prefix="/api/rules",     tags=["rules"])
    app.include_router(clients_router,   prefix="/api/clients",   tags=["clients"])

    return app


app = create_app()
