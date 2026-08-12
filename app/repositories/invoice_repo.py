"""Repository for `invoices` (approved / finalized)."""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import InvoiceStatus
from app.models.invoice import Invoice


async def get_by_id(session: AsyncSession, invoice_id: uuid.UUID) -> Invoice | None:
    return await session.get(Invoice, invoice_id)


async def get_by_invoice_no(session: AsyncSession, invoice_no: str) -> Invoice | None:
    result = await session.execute(
        select(Invoice).where(Invoice.invoice_no == invoice_no).limit(1)
    )
    return result.scalar_one_or_none()


async def list_paginated(
    session: AsyncSession,
    *,
    client_id: uuid.UUID | None = None,
    claim_id: uuid.UUID | None = None,
    status: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Invoice], int]:
    """Paginated + filterable listing. Returns (rows, total_count).

    Ordering: newest first by approved_at (which is set on APPROVED).
    """
    from sqlalchemy import and_, func

    conditions = []
    if client_id is not None:
        conditions.append(Invoice.client_id == client_id)
    if claim_id is not None:
        conditions.append(Invoice.claim_id == claim_id)
    if status:
        conditions.append(Invoice.status == status)
    if from_date is not None:
        conditions.append(Invoice.billing_period_end >= from_date)
    if to_date is not None:
        conditions.append(Invoice.billing_period_start <= to_date)

    where_clause = and_(*conditions) if conditions else None

    # Total count for pagination header
    count_stmt = select(func.count(Invoice.id))
    if where_clause is not None:
        count_stmt = count_stmt.where(where_clause)
    total = (await session.execute(count_stmt)).scalar_one()

    # Page of rows
    rows_stmt = select(Invoice).order_by(Invoice.approved_at.desc())
    if where_clause is not None:
        rows_stmt = rows_stmt.where(where_clause)
    rows_stmt = rows_stmt.limit(limit).offset(offset)
    rows = (await session.execute(rows_stmt)).scalars().all()

    return list(rows), int(total)


async def list_for_claim(
    session: AsyncSession, claim_id: uuid.UUID,
) -> list[Invoice]:
    """All invoices for one claim — used by review UI's history sidebar."""
    result = await session.execute(
        select(Invoice)
        .where(Invoice.claim_id == claim_id)
        .order_by(Invoice.approved_at.desc())
    )
    return list(result.scalars().all())


async def next_invoice_number(session: AsyncSession) -> str:
    """Deterministic invoice numbering: INV-YYYY-NNNNN.
    Resets each year. Uses a MAX() lookup — for higher volumes we'd move
    to a proper sequence, but at GNC's monthly cadence this is safe."""
    year = datetime.now(UTC).year
    prefix = f"INV-{year}-"
    result = await session.execute(
        select(Invoice.invoice_no).where(Invoice.invoice_no.like(f"{prefix}%"))
    )
    used = [r[0] for r in result.fetchall()]
    max_seq = 0
    for num in used:
        try:
            seq = int(num.rsplit("-", 1)[-1])
            max_seq = max(max_seq, seq)
        except ValueError:
            continue
    return f"{prefix}{max_seq + 1:05d}"


async def create(
    session: AsyncSession,
    *,
    draft_id: uuid.UUID,
    claim_id: uuid.UUID,
    client_id: uuid.UUID,
    invoice_no: str,
    snapshot_data: dict[str, Any],
    excel_path: str,
    excel_file_size: int,
    amount: Decimal,
    billing_period_start: date,
    billing_period_end: date,
    approved_by: uuid.UUID | None = None,
    currency: str = "CAD",
    ai_confidence_avg: Decimal = Decimal("0"),
    emails_processed: int = 0,
    attachments_count: int = 0,
    manual_overrides: int = 0,
) -> Invoice:
    """Create a finalized (approved) invoice row.

    `snapshot_data` is an immutable JSONB blob containing everything the
    draft had at approval time: client_details, insured_details, loss_details,
    line_items, invoice_date, invoice_no, gnc_file_no. This means the
    invoice tells its own story even if the source claim/client/draft is
    later edited or deleted.
    """
    inv = Invoice(
        draft_id=draft_id, claim_id=claim_id, client_id=client_id,
        invoice_no=invoice_no,
        snapshot_data=snapshot_data,
        excel_path=excel_path,
        excel_file_size=excel_file_size,
        amount=amount, currency=currency,
        billing_period_start=billing_period_start,
        billing_period_end=billing_period_end,
        status=InvoiceStatus.APPROVED.value,
        approved_by=approved_by,
        approved_at=datetime.now(UTC),
        ai_confidence_avg=ai_confidence_avg,
        emails_processed=emails_processed,
        attachments_count=attachments_count,
        manual_overrides=manual_overrides,
    )
    session.add(inv)
    await session.flush()
    return inv
