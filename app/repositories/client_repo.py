"""Repository for `clients`."""
from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client


async def get_by_id(session: AsyncSession, client_id: uuid.UUID) -> Client | None:
    return await session.get(Client, client_id)


async def list_active(session: AsyncSession) -> Sequence[Client]:
    """All non-deleted clients, ordered by name — used for dropdowns."""
    result = await session.execute(
        select(Client)
        .where(Client.deleted_at.is_(None), Client.is_active.is_(True))
        .order_by(Client.name)
    )
    return result.scalars().all()


async def list_all(session: AsyncSession) -> Sequence[Client]:
    """All non-deleted clients (active + inactive) for admin views."""
    result = await session.execute(
        select(Client).where(Client.deleted_at.is_(None)).order_by(Client.name)
    )
    return result.scalars().all()
