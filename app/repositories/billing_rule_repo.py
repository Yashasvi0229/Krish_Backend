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


async def get_by_id(session: AsyncSession, rule_id):
    """Load a rule by its UUID."""
    return await session.get(BillingRule, rule_id)


async def create(session: AsyncSession, **fields):
    """Insert a new BillingRule. Version defaults to 1; caller may bump."""
    rule = BillingRule(
        client_scope="global",
        client_ids=[],
        comments="",
        version=1,
        **fields,
    )
    session.add(rule)
    await session.flush()
    return rule


async def update(session: AsyncSession, rule, **fields):
    """Apply partial patch. Bumps version so audit trail reflects the edit."""
    changed = False
    for key, value in fields.items():
        if hasattr(rule, key) and getattr(rule, key) != value:
            setattr(rule, key, value)
            changed = True
    if changed:
        rule.version += 1
    await session.flush()
    return rule


async def delete(session: AsyncSession, rule):
    """Hard-delete only allowed when the rule has never been used in a
    draft; otherwise the caller should just flip is_active=False.
    We keep the check simple here — callers can inspect line_items usage
    via the invoice_drafts JSONB if they want to be strict."""
    await session.delete(rule)
    await session.flush()
