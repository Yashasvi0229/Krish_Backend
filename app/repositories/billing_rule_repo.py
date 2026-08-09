"""Repository for `billing_rules`.

DB storage is a mirror of the RULES dict in services/billing_service.py.
The service is the runtime source of truth (fast, in-memory); the DB
version supports future admin UI edits and audit history.
"""
from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ClientScope
from app.models.billing_rule import BillingRule


async def list_active_global(session: AsyncSession) -> Sequence[BillingRule]:
    result = await session.execute(
        select(BillingRule).where(
            BillingRule.is_active.is_(True),
            BillingRule.client_scope == ClientScope.GLOBAL.value,
        ).order_by(BillingRule.code)
    )
    return result.scalars().all()


async def get_by_code(session: AsyncSession, code: str) -> BillingRule | None:
    result = await session.execute(
        select(BillingRule).where(BillingRule.code == code).limit(1)
    )
    return result.scalar_one_or_none()


async def get_by_codes(session: AsyncSession, codes: list[str]) -> dict[str, BillingRule]:
    """Bulk lookup by rule codes — for computing many lines at once."""
    if not codes:
        return {}
    result = await session.execute(
        select(BillingRule).where(BillingRule.code.in_(codes))
    )
    return {r.code: r for r in result.scalars().all()}
