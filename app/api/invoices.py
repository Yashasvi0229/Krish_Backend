"""
Invoice draft + finalized invoice endpoints.

    GET  /api/drafts/{draft_id}                — view draft
    POST /api/drafts/{draft_id}/approve        — generate .xlsx + finalize
    GET  /api/invoices/{invoice_id}            — get finalized invoice
    GET  /api/invoices/{invoice_id}/download   — download the .xlsx
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentAdmin, get_current_admin
from app.core.constants import DraftStatus
from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.database import get_db
from app.models.claim import Claim
from app.models.client import Client
from app.repositories import claim_repo, draft_repo, invoice_repo
from app.schemas.invoice import GenerateInvoiceRequest, InvoiceDraftDetail
from app.services.invoice_service import render_invoice_xlsx, save_invoice_file
from app.utils.file_storage import storage

log = get_logger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Draft view
# ---------------------------------------------------------------------------
@router.get("/drafts/{draft_id}", response_model=InvoiceDraftDetail)
async def get_draft(
    draft_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
) -> InvoiceDraftDetail:
    d = await draft_repo.get_by_id(db, draft_id)
    if d is None:
        raise NotFoundError(f"Draft {draft_id} not found.")
    return InvoiceDraftDetail.model_validate(d)


# ---------------------------------------------------------------------------
# Draft → Invoice
# ---------------------------------------------------------------------------
@router.post("/drafts/{draft_id}/approve")
async def approve_draft(
    draft_id: uuid.UUID,
    payload: GenerateInvoiceRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
) -> dict:
    """Finalize a draft: generate the .xlsx, persist as Invoice, mark
    draft APPROVED."""
    draft = await draft_repo.get_by_id(db, draft_id)
    if draft is None:
        raise NotFoundError(f"Draft {draft_id} not found.")
    if draft.status not in (DraftStatus.DRAFT.value, DraftStatus.PENDING_PM.value,
                            DraftStatus.PENDING_HOUR_VERIFY.value,
                            DraftStatus.PENDING_RS.value):
        raise ConflictError(
            f"Draft is in status '{draft.status}' — cannot approve. "
            f"Only DRAFT/PENDING_* drafts may be approved."
        )

    client = await db.get(Client, draft.client_id)
    hourly_rate = _resolve_hourly_rate(client)

    # ---- Render the workbook ----
    xlsx_bytes = render_invoice_xlsx(
        invoice_no=draft.invoice_no,
        invoice_date=draft.invoice_date,
        gnc_file_no=draft.gnc_file_no,
        client_details=draft.client_details,
        insured_details=draft.insured_details,
        loss_details=draft.loss_details,
        line_items=list(draft.line_items or []),
        billing_period_start=draft.billing_period_start,
        billing_period_end=draft.billing_period_end,
        subtotal=Decimal(str(draft.subtotal)),
        grand_total=Decimal(str(draft.grand_total)),
        hourly_rate=hourly_rate,
        currency=draft.currency,
    )
    rel_path, size = save_invoice_file(draft.invoice_no, draft.gnc_file_no, xlsx_bytes)

    # ---- Snapshot everything ----
    snapshot = {
        "invoice_no": draft.invoice_no,
        "invoice_date": draft.invoice_date.isoformat(),
        "gnc_file_no": draft.gnc_file_no,
        "client_details": draft.client_details,
        "insured_details": draft.insured_details,
        "loss_details": draft.loss_details,
        "line_items": list(draft.line_items or []),
        "subtotal": float(draft.subtotal),
        "grand_total": float(draft.grand_total),
        "currency": draft.currency,
        "notes": payload.approve_notes,
        "approved_from_draft_id": str(draft.id),
    }

    invoice = await invoice_repo.create(
        db,
        draft_id=draft.id, claim_id=draft.claim_id, client_id=draft.client_id,
        invoice_no=draft.invoice_no,
        snapshot_data=snapshot,
        excel_path=rel_path,
        excel_file_size=size,
        amount=Decimal(str(draft.grand_total)),
        billing_period_start=draft.billing_period_start,
        billing_period_end=draft.billing_period_end,
        currency=draft.currency,
        emails_processed=draft.total_emails,
        attachments_count=len([
            li for li in (draft.line_items or [])
            if li.get("source_attachment_id")
        ]),
    )
    await draft_repo.update_status(db, draft.id, DraftStatus.APPROVED,
                                    approved_invoice_id=invoice.id)
    await db.commit()

    log.info("invoice_approved",
             invoice_id=str(invoice.id), invoice_no=invoice.invoice_no,
             amount=float(invoice.amount))

    return {
        "invoice_id": str(invoice.id),
        "invoice_no": invoice.invoice_no,
        "amount": float(invoice.amount),
        "currency": invoice.currency,
        "excel_download_url": f"/api/invoices/{invoice.id}/download",
    }


# ---------------------------------------------------------------------------
# Invoice view + download
# ---------------------------------------------------------------------------
@router.get("/invoices/{invoice_id}")
async def get_invoice(
    invoice_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
) -> dict:
    inv = await invoice_repo.get_by_id(db, invoice_id)
    if inv is None:
        raise NotFoundError(f"Invoice {invoice_id} not found.")
    return {
        "id": str(inv.id),
        "invoice_no": inv.invoice_no,
        "amount": float(inv.amount),
        "currency": inv.currency,
        "status": inv.status,
        "billing_period_start": inv.billing_period_start.isoformat(),
        "billing_period_end": inv.billing_period_end.isoformat(),
        "approved_at": inv.approved_at.isoformat(),
        "snapshot": inv.snapshot_data,
        "excel_download_url": f"/api/invoices/{inv.id}/download",
    }


@router.get("/invoices/{invoice_id}/download")
async def download_invoice(
    invoice_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
) -> FileResponse:
    inv = await invoice_repo.get_by_id(db, invoice_id)
    if inv is None:
        raise NotFoundError(f"Invoice {invoice_id} not found.")
    abs_path = storage.absolute_path(inv.excel_path)
    if not abs_path.exists():
        raise NotFoundError("Invoice file missing on disk.")
    return FileResponse(
        path=str(abs_path),
        filename=f"{inv.invoice_no}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _resolve_hourly_rate(client) -> Decimal:
    if client is None or not client.rate_config:
        return Decimal("150.00")
    raw = (client.rate_config or {}).get("hourly_rate")
    if raw is None:
        return Decimal("150.00")
    try:
        return Decimal(str(raw))
    except Exception:  # noqa: BLE001
        return Decimal("150.00")
