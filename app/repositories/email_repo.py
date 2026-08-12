"""
Repository for the `emails` table.

Key operation: `bulk_upsert_by_gmail_id` — inserts new emails, skips
duplicates (by gmail_message_id). Used by both the periodic sync task and
the on-demand search worker. Returns the emails that were actually
inserted (so callers know what to process further).
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.email import Email


async def get_by_id(session: AsyncSession, email_id: uuid.UUID) -> Email | None:
    result = await session.execute(select(Email).where(Email.id == email_id))
    return result.scalar_one_or_none()


async def get_by_gmail_message_id(
    session: AsyncSession, gmail_message_id: str
) -> Email | None:
    result = await session.execute(
        select(Email).where(Email.gmail_message_id == gmail_message_id)
    )
    return result.scalar_one_or_none()


async def list_by_claim(
    session: AsyncSession,
    claim_id: uuid.UUID,
    limit: int = 500,
) -> Sequence[Email]:
    result = await session.execute(
        select(Email)
        .where(Email.claim_id == claim_id)
        .order_by(Email.date.desc())
        .limit(limit)
    )
    return result.scalars().all()


async def bulk_upsert_by_gmail_id(
    session: AsyncSession, rows: list[dict[str, Any]]
) -> list[uuid.UUID]:
    """
    INSERT ... ON CONFLICT (gmail_message_id) DO NOTHING.

    Returns the IDs of the emails that were actually inserted (not the
    ones we skipped due to conflict). Caller can use this list to know
    which emails need attachment download / AI analysis.
    """
    if not rows:
        return []

    stmt = pg_insert(Email).values(rows)
    stmt = stmt.on_conflict_do_nothing(index_elements=["gmail_message_id"])
    stmt = stmt.returning(Email.id)

    result = await session.execute(stmt)
    inserted_ids = [row[0] for row in result.fetchall()]
    return inserted_ids


async def link_to_claim(
    session: AsyncSession, email_ids: list[uuid.UUID], claim_id: uuid.UUID
) -> None:
    """Attach a set of emails to a claim in one UPDATE."""
    if not email_ids:
        return
    from sqlalchemy import update
    await session.execute(
        update(Email)
        .where(Email.id.in_(email_ids))
        .values(claim_id=claim_id)
    )
