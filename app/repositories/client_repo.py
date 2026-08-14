"""Repository for `clients`."""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client


async def get_by_id(session: AsyncSession, client_id: uuid.UUID) -> Client | None:
    return await session.get(Client, client_id)


async def list_active(session: AsyncSession) -> Sequence[Client]:
    """All non-deleted + active clients, ordered by name."""
    result = await session.execute(
        select(Client)
        .where(Client.deleted_at.is_(None), Client.is_active.is_(True))
        .order_by(Client.name)
    )
    return result.scalars().all()


async def list_all(session: AsyncSession) -> Sequence[Client]:
    """All non-deleted clients (active + inactive)."""
    result = await session.execute(
        select(Client).where(Client.deleted_at.is_(None)).order_by(Client.name)
    )
    return result.scalars().all()


async def create(session: AsyncSession, **fields: Any) -> Client:
    """Insert a new Client. Caller supplies flat kwargs — we pack
    `hourly_rate` into `rate_config` (JSONB) transparently since that's
    where the rest of the codebase reads it from."""
    rate = fields.pop("hourly_rate", None)
    rate_config = {"hourly_rate": float(rate) if rate is not None else 150.0}

    client = Client(
        rate_strategy="flat",
        rate_config=rate_config,
        template_path=fields.pop("template_path", ""),
        **fields,
    )
    session.add(client)
    await session.flush()
    return client


async def update(session: AsyncSession, client: Client, **fields: Any) -> Client:
    """Apply partial patch — only fields explicitly supplied are touched.
    hourly_rate maps into rate_config (JSONB)."""
    if "hourly_rate" in fields:
        rate = fields.pop("hourly_rate")
        rc = dict(client.rate_config or {})
        rc["hourly_rate"] = float(rate) if rate is not None else rc.get("hourly_rate", 150.0)
        client.rate_config = rc
    for key, value in fields.items():
        if hasattr(client, key):
            setattr(client, key, value)
    await session.flush()
    return client


async def soft_delete(session: AsyncSession, client: Client) -> Client:
    """Mark deleted (deleted_at) instead of removing the row — preserves
    historical invoices' client references."""
    client.deleted_at = datetime.now(UTC)
    client.is_active = False
    await session.flush()
    return client
