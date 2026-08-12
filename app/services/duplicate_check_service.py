"""
Duplicate-billing detection.

Prevents the common failure mode: same claim gets analyzed twice within
weeks, producing two invoices for overlapping billing periods, and the
client gets billed twice.

Design:
    * WARN not BLOCK — the reviewer knows their business better than we do.
      A `duplicate_warning` object gets attached to the new draft so the
      review UI can render a banner. Approval is still allowed.
    * Compare against APPROVED invoices only (unfinalized drafts don't
      count — they can be discarded).
    * Period overlap: two periods [a, b] and [c, d] overlap iff a <= d
      AND c <= b. That's the standard interval-overlap check.

Called from the analyze-claim worker after all AI + line items are ready,
before the InvoiceDraft is created.
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import InvoiceStatus
from app.core.logging import get_logger
from app.models.invoice import Invoice

log = get_logger(__name__)


async def check_for_duplicates(
    session: AsyncSession,
    *,
    claim_id: uuid.UUID,
    billing_period_start: date,
    billing_period_end: date,
) -> dict[str, Any] | None:
    """Return a `duplicate_warning` dict if this claim already has an
    APPROVED invoice with overlapping period. None means safe to proceed.

    The returned dict shape is what the InvoiceDraft.duplicate_warning
    column stores — it lands unchanged and the frontend renders it.
    """
    stmt = select(Invoice).where(
        Invoice.claim_id == claim_id,
        Invoice.status == InvoiceStatus.APPROVED.value,
        # Interval overlap: NEW.start <= existing.end AND existing.start <= NEW.end
        Invoice.billing_period_start <= billing_period_end,
        Invoice.billing_period_end >= billing_period_start,
    ).order_by(Invoice.approved_at.desc())

    result = await session.execute(stmt)
    overlapping = result.scalars().all()

    if not overlapping:
        return None

    # Compact summary — enough for the UI banner without dumping snapshots.
    prior = [{
        "invoice_id": str(inv.id),
        "invoice_no": inv.invoice_no,
        "amount": float(inv.amount),
        "billing_period_start": inv.billing_period_start.isoformat(),
        "billing_period_end": inv.billing_period_end.isoformat(),
        "approved_at": inv.approved_at.isoformat(),
    } for inv in overlapping]

    log.warning(
        "duplicate_billing_detected",
        claim_id=str(claim_id),
        matches=len(prior),
        prior_invoice_no=prior[0]["invoice_no"],
    )

    return {
        "kind": "period_overlap",
        "message": (
            f"This claim already has {len(prior)} approved invoice"
            f"{'s' if len(prior) != 1 else ''} whose billing period overlaps "
            f"with {billing_period_start.isoformat()} → "
            f"{billing_period_end.isoformat()}. Review before approving."
        ),
        "matches": prior,
        "detected_at": None,  # server can add if needed
    }
