"""
Health check endpoints.

- `GET /health`             — liveness (is the process running?)
- `GET /api/health`         — same, prefixed under /api for the frontend proxy
- `GET /api/health/ready`   — readiness (are Postgres and Redis reachable?)
"""
from __future__ import annotations

from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db

router = APIRouter(tags=["health"])


@router.get("/health", status_code=status.HTTP_200_OK)
async def health() -> dict[str, Any]:
    """Liveness probe. Returns 200 as long as the process is serving."""
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.app_version,
        "env": settings.app_env,
    }


@router.get("/health/ready", status_code=status.HTTP_200_OK)
async def readiness(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """
    Readiness probe. Checks that:
        * Postgres accepts a `SELECT 1`
        * Redis responds to PING
    Returns per-dependency status so ops can see which one is down.
    """
    checks: dict[str, str] = {}

    # ---- Postgres ----
    try:
        result = await db.execute(text("SELECT 1"))
        result.scalar_one()
        checks["database"] = "ok"
    except Exception as e:  # noqa: BLE001 — health checks report all errors
        checks["database"] = f"error: {type(e).__name__}"

    # ---- Redis ----
    try:
        client = aioredis.from_url(settings.redis_url, decode_responses=True)
        pong = await client.ping()
        await client.aclose()
        checks["redis"] = "ok" if pong else "error: no pong"
    except Exception as e:  # noqa: BLE001
        checks["redis"] = f"error: {type(e).__name__}"

    all_ok = all(v == "ok" for v in checks.values())
    return {"status": "ok" if all_ok else "degraded", "checks": checks}
