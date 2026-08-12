"""
Redis client helpers.

We use Redis for two things (Step 4):
    1. Celery broker + result backend (see app/celery_app.py)
    2. Pub/sub channel for streaming job progress to WebSocket clients

For pub/sub we create one `redis.asyncio.Redis` client per WebSocket
connection (Redis pub/sub isn't multiplexable — each subscriber needs its
own connection). For simple SET/GET we can share a pool via `get_pool()`.
"""
from __future__ import annotations

from typing import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis

from app.config import settings

# Job progress pub/sub channel prefix — combined with job UUID.
JOB_CHANNEL_PREFIX = "gnc:job:"


def job_channel(job_id: str) -> str:
    return f"{JOB_CHANNEL_PREFIX}{job_id}"


# Async client (for FastAPI request handlers and WebSocket)
_async_pool: aioredis.ConnectionPool | None = None


def get_async_client() -> aioredis.Redis:
    """Shared async client. Reuses a connection pool."""
    global _async_pool
    if _async_pool is None:
        _async_pool = aioredis.ConnectionPool.from_url(
            settings.redis_url,
            decode_responses=True,
            max_connections=20,
        )
    return aioredis.Redis(connection_pool=_async_pool)


@asynccontextmanager
async def new_pubsub_subscription(channel: str) -> AsyncIterator[aioredis.client.PubSub]:
    """
    Context manager that yields a fresh PubSub subscribed to `channel`.

    Redis pub/sub needs a dedicated connection per subscriber — sharing
    would mix messages across subscribers. Callers use this like:

        async with new_pubsub_subscription(job_channel(id)) as ps:
            async for msg in ps.listen():
                ...
    """
    client = aioredis.from_url(settings.redis_url, decode_responses=True)
    pubsub = client.pubsub()
    await pubsub.subscribe(channel)
    try:
        yield pubsub
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()
        await client.aclose()
