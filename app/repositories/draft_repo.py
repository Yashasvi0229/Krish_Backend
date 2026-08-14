"""Repository for `invoice_drafts`."""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import DraftStatus
from app.models.invoice_draft import InvoiceDraft


async def get_by_id(session: AsyncSession, draft_id: uuid.UUID) -> InvoiceDraft | None:
    return await session.get(InvoiceDraft, draft_id)


async def list_by_claim(
    session: AsyncSession, claim_id: uuid.UUID
) -> list[InvoiceDraft]:
    result = await session.execute(
        select(InvoiceDraft)
        .where(InvoiceDraft.claim_id == claim_id)
        .order_by(InvoiceDraft.created_at.desc())
    )
    return list(result.scalars().all())


async def create(
    session: AsyncSession,
    *,
    claim_id: uuid.UUID,
    client_id: uuid.UUID,
    job_id: uuid.UUID | None,
    created_by: uuid.UUID | None,
    invoice_no: str,
    invoice_date: date,
    gnc_file_no: str,
    client_details: dict[str, Any],
    insured_details: dict[str, Any],
    loss_details: dict[str, Any],
    line_items: list[dict[str, Any]],
    billing_period_start: date,
    billing_period_end: date,
    subtotal: Decimal,
    grand_total: Decimal,
    currency: str = "CAD",
    total_emails: int = 0,
    emails_reviewed: int = 0,
    duplicate_warning: dict[str, Any] | None = None,
) -> InvoiceDraft:
    d = InvoiceDraft(
        claim_id=claim_id, client_id=client_id, job_id=job_id,
        created_by=created_by,
        status=DraftStatus.DRAFT.value,
        invoice_no=invoice_no, invoice_date=invoice_date,
        gnc_file_no=gnc_file_no,
        client_details=client_details, insured_details=insured_details,
        loss_details=loss_details, line_items=line_items,
        billing_period_start=billing_period_start,
        billing_period_end=billing_period_end,
        subtotal=subtotal, grand_total=grand_total,
        currency=currency,
        total_emails=total_emails, emails_reviewed=emails_reviewed,
        duplicate_warning=duplicate_warning,
    )
    session.add(d)
    await session.flush()
    return d


async def update_status(
    session: AsyncSession, draft_id: uuid.UUID,
    status: DraftStatus, approved_invoice_id: uuid.UUID | None = None,
) -> None:
    draft = await get_by_id(session, draft_id)
    if not draft:
        return
    draft.status = status.value
    if approved_invoice_id is not None:
        draft.approved_invoice_id = approved_invoice_id


async def list_paginated(
    session: AsyncSession,
    *,
    status: str | None = None,
    statuses: list[str] | None = None,
    client_id: uuid.UUID | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[InvoiceDraft], int]:
    """Paginated list across all drafts. Filters: status (single), statuses
    (list — takes precedence), client_id. Newest first.

    Used by the dashboard's "Pending Review" section — we can query
    multiple statuses at once (DRAFT + PENDING_*) with a single call.
    """
    from sqlalchemy import and_, func

    conditions = []
    if statuses:
        conditions.append(InvoiceDraft.status.in_(statuses))
    elif status:
        conditions.append(InvoiceDraft.status == status)
    if client_id is not None:
        conditions.append(InvoiceDraft.client_id == client_id)

    where = and_(*conditions) if conditions else None

    count_stmt = select(func.count(InvoiceDraft.id))
    if where is not None:
        count_stmt = count_stmt.where(where)
    total = (await session.execute(count_stmt)).scalar_one()

    rows_stmt = select(InvoiceDraft).order_by(InvoiceDraft.created_at.desc())
    if where is not None:
        rows_stmt = rows_stmt.where(where)
    rows_stmt = rows_stmt.limit(limit).offset(offset)
    rows = (await session.execute(rows_stmt)).scalars().all()
    return list(rows), int(total)
