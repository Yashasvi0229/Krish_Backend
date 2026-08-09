"""
Repository for the `attachments` table.

Attachment dedup happens at TWO levels:
    1. `file_hash` UNIQUE ish — same PDF attached twice reuses the same
       `storage_path` (saved on disk once). But we still create one
       attachment row per (email, file) so we know WHICH emails include
       the same file.
    2. Extracted text is stored once at `extracted_text_path` — also
       keyed by file_hash — and re-used across all attachment rows.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attachment import Attachment


async def find_by_file_hash(
    session: AsyncSession, file_hash: str
) -> Attachment | None:
    """Return ANY existing attachment row for this file_hash. Used to
    reuse the storage_path / extracted_text_path — the file itself is
    already on disk, so no need to re-download."""
    result = await session.execute(
        select(Attachment).where(Attachment.file_hash == file_hash).limit(1)
    )
    return result.scalar_one_or_none()


async def list_by_email(
    session: AsyncSession, email_id: uuid.UUID
) -> Sequence[Attachment]:
    result = await session.execute(
        select(Attachment).where(Attachment.email_id == email_id)
    )
    return result.scalars().all()


async def get_by_id(session: AsyncSession, att_id: uuid.UUID) -> Attachment | None:
    return await session.get(Attachment, att_id)


async def bulk_insert(
    session: AsyncSession, rows: list[dict[str, Any]]
) -> list[uuid.UUID]:
    """Simple bulk insert. Callers are expected to have already deduped."""
    if not rows:
        return []
    stmt = pg_insert(Attachment).values(rows).returning(Attachment.id)
    result = await session.execute(stmt)
    return [row[0] for row in result.fetchall()]
