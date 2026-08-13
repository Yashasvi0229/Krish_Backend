"""Dashboard aggregate stats.

    GET /api/dashboard/stats
        {
          "total_this_month": {"count": 12, "amount": 45680.50},
          "pending_review":   4,    # any DraftStatus.PENDING_*
          "approved":         8,    # InvoiceStatus.APPROVED
          "flagged":          3     # drafts with any line where is_flagged=True
        }

Read-only aggregation over invoices + drafts. Cheap enough at GNC's monthly
cadence that we don't cache — recompute per request.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentAdmin, get_current_admin
from app.core.constants import DraftStatus, InvoiceStatus
from app.database import get_db
from app.models.invoice import Invoice
from app.models.invoice_draft import InvoiceDraft

router = APIRouter()


@router.get("/stats")
async def dashboard_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
) -> dict:
    """Aggregate stats for the dashboard's four cards."""
    now = datetime.now(UTC)
    month_start = datetime(now.year, now.month, 1, tzinfo=UTC)

    # 1. Total this month — count + sum of approved invoice amounts
    month_stmt = select(
        func.count(Invoice.id),
        func.coalesce(func.sum(Invoice.amount), 0),
    ).where(
        Invoice.status == InvoiceStatus.APPROVED.value,
        Invoice.approved_at >= month_start,
    )
    month_count, month_amount = (await db.execute(month_stmt)).one()

    # 2. Pending review — any draft in a PENDING_* stage
    pending_statuses = [
        DraftStatus.PENDING_PM.value,
        DraftStatus.PENDING_HOUR_VERIFY.value,
        DraftStatus.PENDING_RS.value,
    ]
    pending_count = (await db.execute(
        select(func.count(InvoiceDraft.id))
        .where(InvoiceDraft.status.in_(pending_statuses))
    )).scalar_one()

    # 3. Approved — all-time approved invoices
    approved_count = (await db.execute(
        select(func.count(Invoice.id))
        .where(Invoice.status == InvoiceStatus.APPROVED.value)
    )).scalar_one()

    # 4. Flagged — drafts (DRAFT or PENDING) with any line flagged for review.
    # We use a JSONB EXISTS check to avoid pulling every draft into Python.
    from sqlalchemy import text
    flagged_count = (await db.execute(text("""
        SELECT COUNT(DISTINCT d.id)
        FROM invoice_drafts d,
             jsonb_array_elements(d.line_items) AS li
        WHERE d.status IN ('DRAFT', 'PENDING_PM', 'PENDING_HOUR_VERIFY', 'PENDING_RS')
          AND (li->>'is_flagged')::boolean = TRUE
    """))).scalar_one()

    return {
        "total_this_month": {
            "count": int(month_count),
            "amount": float(month_amount),
        },
        "pending_review": int(pending_count),
        "approved":       int(approved_count),
        "flagged":        int(flagged_count),
    }
