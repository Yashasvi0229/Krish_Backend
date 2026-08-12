"""
Repository for the `gmail_connection` singleton row.

Only one row is ever expected (see the CHECK constraint on the model).
This repo enforces that at the API level: `upsert_singleton()` reads the
current row and INSERTs or UPDATEs so callers don't have to think about it.

Everything here is async — call inside a request handler with a session
from `Depends(get_db)`, or inside a Celery task with a fresh session.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.gmail_connection import GmailConnection


SINGLETON_KEY = "default"


async def get_singleton(session: AsyncSession) -> GmailConnection | None:
    """Return the (only) GmailConnection row, or None if never connected."""
    stmt = select(GmailConnection).where(
        GmailConnection.singleton_key == SINGLETON_KEY
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def upsert_singleton(
    session: AsyncSession,
    **fields: Any,
) -> GmailConnection:
    """Insert-or-update the singleton row.

    `fields` are ORM column values (e.g. `email=`, `refresh_token_encrypted=`).
    Caller is responsible for committing the session.
    """
    existing = await get_singleton(session)
    if existing is None:
        row = GmailConnection(singleton_key=SINGLETON_KEY, **fields)
        session.add(row)
        await session.flush()   # populate row.id / defaults
        return row

    for key, value in fields.items():
        setattr(existing, key, value)
    await session.flush()
    return existing


async def delete_singleton(session: AsyncSession) -> bool:
    """Remove the singleton row (disconnect Gmail). Returns True if a row was removed."""
    existing = await get_singleton(session)
    if existing is None:
        return False
    await session.delete(existing)
    await session.flush()
    return True
