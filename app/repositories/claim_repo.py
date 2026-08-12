"""
Repository for the `claims` table.

`find_or_create` is the key operation used by the on-demand search flow:
when a user searches for claim "123-45" under client X, we upsert a claim
row so subsequent emails/attachments/drafts have something to hang off.
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ClaimStatus
from app.models.claim import Claim


async def get_by_id(session: AsyncSession, claim_id: uuid.UUID) -> Claim | None:
    return await session.get(Claim, claim_id)


async def find_by_client_and_claim_no(
    session: AsyncSession, client_id: uuid.UUID, claim_no: str
) -> Claim | None:
    result = await session.execute(
        select(Claim).where(
            Claim.client_id == client_id, Claim.claim_no == claim_no
        )
    )
    return result.scalar_one_or_none()


async def find_by_gnc_file_no(
    session: AsyncSession, gnc_file_no: str
) -> Claim | None:
    result = await session.execute(
        select(Claim).where(Claim.gnc_file_no == gnc_file_no)
    )
    return result.scalar_one_or_none()


async def search_by_identifiers(
    session: AsyncSession,
    claim_no: str | None = None,
    file_name: str | None = None,
    gnc_file_no: str | None = None,
) -> Sequence[Claim]:
    """Find claims matching ANY of the supplied identifiers. Callers should
    disambiguate (or ask user to pick) when multiple rows come back."""
    conditions = []
    if claim_no:
        conditions.append(Claim.claim_no == claim_no)
    if file_name:
        conditions.append(Claim.file_name == file_name)
    if gnc_file_no:
        conditions.append(Claim.gnc_file_no == gnc_file_no)
    if not conditions:
        return []
    result = await session.execute(select(Claim).where(or_(*conditions)))
    return result.scalars().all()


async def create(
    session: AsyncSession,
    *,
    client_id: uuid.UUID,
    gnc_file_no: str,
    claim_no: str,
    file_name: str,
    loss_type: str = "Unknown",
    insured_details: dict[str, Any] | None = None,
) -> Claim:
    claim = Claim(
        client_id=client_id,
        gnc_file_no=gnc_file_no,
        claim_no=claim_no,
        file_name=file_name,
        loss_type=loss_type,
        insured_details=insured_details or {},
        status=ClaimStatus.ACTIVE.value,
    )
    session.add(claim)
    await session.flush()
    return claim
