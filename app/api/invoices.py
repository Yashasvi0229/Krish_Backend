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

    # Render's free-tier filesystem is ephemeral — every redeploy wipes
    # /tmp/gnc_storage. When the on-disk file is gone we regenerate it
    # from the invoice's snapshot_data (which is the source of truth in
    # Postgres and never lost). The regenerated bytes are identical for
    # a given snapshot, so downstream users can't tell the difference.
    if not abs_path.exists():
        from app.services.invoice_service import (
            render_invoice_xlsx, save_invoice_file,
        )
        from datetime import date as _date
        from decimal import Decimal as _Decimal

        snap = inv.snapshot_data or {}
        active_lines = [
            li for li in (snap.get("line_items") or []) if not li.get("removed")
        ]
        # Parse invoice_date back into a date object (snapshot stores ISO string)
        inv_date_raw = snap.get("invoice_date")
        try:
            inv_date = _date.fromisoformat(inv_date_raw) if inv_date_raw else _date.today()
        except (TypeError, ValueError):
            inv_date = _date.today()

        # Rate is embedded in line totals already; use the first line's rate
        # as the header rate. Falls back to 150 if snapshot is minimal.
        header_rate = _Decimal(str(
            (active_lines[0].get("rate") if active_lines else None) or 150
        ))

        xlsx_bytes = render_invoice_xlsx(
            invoice_no=inv.invoice_no,
            invoice_date=inv_date,
            gnc_file_no=snap.get("gnc_file_no", ""),
            client_details=snap.get("client_details") or {},
            insured_details=snap.get("insured_details") or {},
            loss_details=snap.get("loss_details") or {},
            line_items=active_lines,
            billing_period_start=inv.billing_period_start,
            billing_period_end=inv.billing_period_end,
            subtotal=_Decimal(str(snap.get("subtotal") or inv.amount)),
            grand_total=_Decimal(str(inv.amount)),
            hourly_rate=header_rate,
            currency=inv.currency,
        )
        rel_path, size = save_invoice_file(
            inv.invoice_no, snap.get("gnc_file_no", "unknown"), xlsx_bytes,
        )
        # Update the row so next request finds the file without regenerating.
        inv.excel_path = rel_path
        inv.excel_file_size = size
        await db.commit()
        abs_path = storage.absolute_path(rel_path)

    return FileResponse(
        path=str(abs_path),
        filename=f"{inv.invoice_no}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _resolve_hourly_rate(client) -> Decimal:
    """Return the client's hourly rate — falls back to $150 if unset OR zero.
    Must stay in sync with the same helper in app/workers/ai_tasks.py."""
    if client is None or not client.rate_config:
        return Decimal("150.00")
    raw = (client.rate_config or {}).get("hourly_rate")
    if raw is None:
        return Decimal("150.00")
    try:
        rate = Decimal(str(raw))
    except Exception:  # noqa: BLE001
        return Decimal("150.00")
    if rate <= 0:
        return Decimal("150.00")
    return rate


# ===========================================================================
# STEP 6 — Line-item editing (DRAFT/REJECTED status only)
# ===========================================================================
from datetime import date as _date_type  # noqa: E402 — grouped with step 6

from app.schemas.draft_edit import (       # noqa: E402
    AdvanceStageRequest,
    InvoiceListResponse,
    LineItemAdd,
    LineItemDelete,
    LineItemEdit,
    RejectRequest,
    SubmitForReviewRequest,
)
from app.services import (                 # noqa: E402
    draft_edit_service,
    workflow_service,
)


@router.patch("/drafts/{draft_id}/line-items/{line_number}")
async def edit_line(
    draft_id: uuid.UUID,
    line_number: int,
    payload: LineItemEdit,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],
) -> dict:
    """Modify one line item on an editable draft. Recomputes totals."""
    draft = await draft_edit_service.edit_line_item(
        db, draft_id, line_number, payload, user_id=admin.email,
    )
    await db.commit()
    return {
        "draft_id": str(draft.id),
        "status": draft.status,
        "subtotal": float(draft.subtotal),
        "grand_total": float(draft.grand_total),
        "line_number": line_number,
        "message": "Line item updated.",
    }


@router.post("/drafts/{draft_id}/line-items")
async def add_line(
    draft_id: uuid.UUID,
    payload: LineItemAdd,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],
) -> dict:
    """Add a new line item — used when the AI missed a billable event."""
    d = await draft_repo.get_by_id(db, draft_id)
    if d is None:
        raise NotFoundError(f"Draft {draft_id} not found.")

    client = await db.get(Client, d.client_id)
    rate = _resolve_hourly_rate(client)

    draft = await draft_edit_service.add_line_item(
        db, draft_id, payload, user_id=admin.email, default_rate=rate,
    )
    await db.commit()
    return {
        "draft_id": str(draft.id),
        "status": draft.status,
        "subtotal": float(draft.subtotal),
        "grand_total": float(draft.grand_total),
        "message": "Line item added.",
    }


@router.delete("/drafts/{draft_id}/line-items/{line_number}")
async def delete_line(
    draft_id: uuid.UUID,
    line_number: int,
    payload: LineItemDelete,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],
) -> dict:
    """Soft-delete a line item. The row stays in the JSON for audit."""
    draft = await draft_edit_service.delete_line_item(
        db, draft_id, line_number, payload, user_id=admin.email,
    )
    await db.commit()
    return {
        "draft_id": str(draft.id),
        "status": draft.status,
        "subtotal": float(draft.subtotal),
        "grand_total": float(draft.grand_total),
        "line_number": line_number,
        "message": "Line item removed.",
    }


@router.post("/drafts/{draft_id}/line-items/{line_number}/restore")
async def restore_line(
    draft_id: uuid.UUID,
    line_number: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],
) -> dict:
    """Undo a soft-delete."""
    draft = await draft_edit_service.restore_line_item(
        db, draft_id, line_number, user_id=admin.email,
    )
    await db.commit()
    return {
        "draft_id": str(draft.id),
        "status": draft.status,
        "subtotal": float(draft.subtotal),
        "grand_total": float(draft.grand_total),
        "line_number": line_number,
        "message": "Line item restored.",
    }


# ===========================================================================
# STEP 6 — Multi-stage workflow
# ===========================================================================
@router.post("/drafts/{draft_id}/submit-for-review")
async def submit_for_review(
    draft_id: uuid.UUID,
    payload: SubmitForReviewRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],
) -> dict:
    """DRAFT → PENDING_PM. The preparer's final action."""
    draft = await workflow_service.submit_for_review(
        db, draft_id, user_id=admin.email, note=payload.note,
    )
    await db.commit()
    return {
        "draft_id": str(draft.id),
        "status": draft.status,
        "stage": workflow_service.STAGE_LABEL.get(draft.status, draft.status),
        "message": "Submitted for review.",
    }


@router.post("/drafts/{draft_id}/advance")
async def advance_stage(
    draft_id: uuid.UUID,
    payload: AdvanceStageRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],
) -> dict:
    """Advance the draft one stage — the final advance from PENDING_RS
    generates the Excel invoice."""
    draft, invoice_id = await workflow_service.advance_stage(
        db, draft_id, user_id=admin.email, note=payload.note,
    )
    await db.commit()
    result: dict[str, object] = {
        "draft_id": str(draft.id),
        "status": draft.status,
        "stage": workflow_service.STAGE_LABEL.get(draft.status, draft.status),
        "message": f"Advanced to {draft.status}.",
    }
    if invoice_id is not None:
        result["invoice_id"] = str(invoice_id)
        result["excel_download_url"] = f"/api/invoices/{invoice_id}/download"
        result["message"] = "Draft approved and invoice generated."
    return result


@router.post("/drafts/{draft_id}/reject")
async def reject_draft(
    draft_id: uuid.UUID,
    payload: RejectRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],
) -> dict:
    """Reject the draft from any PENDING_* stage. Reason required."""
    draft = await workflow_service.reject(
        db, draft_id,
        user_id=admin.email,
        reason=payload.reason,
        return_to_draft=payload.return_to_draft,
    )
    await db.commit()
    return {
        "draft_id": str(draft.id),
        "status": draft.status,
        "stage": workflow_service.STAGE_LABEL.get(draft.status, draft.status),
        "message": ("Draft rejected and reopened for editing."
                    if payload.return_to_draft
                    else "Draft rejected."),
    }


@router.post("/drafts/{draft_id}/reopen")
async def reopen_draft(
    draft_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],
) -> dict:
    """REJECTED → DRAFT. Admin action to reopen a rejected draft."""
    draft = await workflow_service.reopen(db, draft_id, user_id=admin.email)
    await db.commit()
    return {
        "draft_id": str(draft.id),
        "status": draft.status,
        "message": "Draft reopened for editing.",
    }


@router.get("/drafts/{draft_id}/history")
async def draft_history(
    draft_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],
) -> dict:
    """Full approval + edit audit trail for a draft."""
    draft = await draft_repo.get_by_id(db, draft_id)
    if draft is None:
        raise NotFoundError(f"Draft {draft_id} not found.")
    return {
        "draft_id": str(draft.id),
        "status": draft.status,
        "stage": workflow_service.STAGE_LABEL.get(draft.status, draft.status),
        "history": list(draft.approval_history or []),
        "rejected_reason": draft.rejected_reason,
    }


# ===========================================================================
# STEP 6 — Invoice listing / history
# ===========================================================================
@router.get("/invoices", response_model=InvoiceListResponse)
async def list_invoices(
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],
    client_id: uuid.UUID | None = None,
    claim_id: uuid.UUID | None = None,
    status: str | None = None,
    from_date: _date_type | None = None,
    to_date: _date_type | None = None,
    limit: int = 20,
    offset: int = 0,
) -> InvoiceListResponse:
    """Paginated invoice history. Filters: client_id, claim_id, status,
    date range (billing period overlap). Newest first."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    rows, total = await invoice_repo.list_paginated(
        db,
        client_id=client_id, claim_id=claim_id, status=status,
        from_date=from_date, to_date=to_date,
        limit=limit, offset=offset,
    )

    # Enrich with denormalized fields for list display.
    items = []
    for inv in rows:
        snap = inv.snapshot_data or {}
        client_details = snap.get("client_details") or {}
        loss_details = snap.get("loss_details") or {}
        insured_details = snap.get("insured_details") or {}
        items.append({
            "id": inv.id,
            "invoice_no": inv.invoice_no,
            "claim_id": inv.claim_id,
            "client_id": inv.client_id,
            "amount": inv.amount,
            "currency": inv.currency,
            "status": inv.status,
            "billing_period_start": inv.billing_period_start.isoformat(),
            "billing_period_end": inv.billing_period_end.isoformat(),
            "approved_at": inv.approved_at.isoformat(),
            "client_name": client_details.get("name"),
            "claim_no": loss_details.get("claim_no"),
            "gnc_file_no": snap.get("gnc_file_no"),
            "insured_name": insured_details.get("insured_name"),
        })

    return InvoiceListResponse(
        total=total, limit=limit, offset=offset, items=items,
    )


@router.get("/claims/{claim_id}/invoices")
async def list_claim_invoices(
    claim_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],
) -> list[dict]:
    """All invoices ever generated for a specific claim — for the review
    UI's history sidebar."""
    rows = await invoice_repo.list_for_claim(db, claim_id)
    return [
        {
            "id": str(inv.id),
            "invoice_no": inv.invoice_no,
            "amount": float(inv.amount),
            "currency": inv.currency,
            "status": inv.status,
            "billing_period_start": inv.billing_period_start.isoformat(),
            "billing_period_end": inv.billing_period_end.isoformat(),
            "approved_at": inv.approved_at.isoformat(),
        } for inv in rows
    ]


@router.get("/claims/{claim_id}/drafts")
async def list_claim_drafts(
    claim_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],
) -> list[dict]:
    """All drafts ever generated for a claim — includes in-review + rejected."""
    drafts = await draft_repo.list_by_claim(db, claim_id)
    return [
        {
            "id": str(d.id),
            "status": d.status,
            "stage": workflow_service.STAGE_LABEL.get(d.status, d.status),
            "invoice_no": d.invoice_no,
            "grand_total": float(d.grand_total),
            "currency": d.currency,
            "created_at": d.created_at.isoformat() if d.created_at else None,
            "has_duplicate_warning": d.duplicate_warning is not None,
            "approved_invoice_id": (
                str(d.approved_invoice_id) if d.approved_invoice_id else None
            ),
        } for d in drafts
    ]


# ===========================================================================
# Invoice-level actions (delete, duplicate)
# ===========================================================================
from app.schemas.invoice_actions import (           # noqa: E402
    InvoiceDeleteRequest,
    InvoiceDuplicateRequest,
)


@router.delete("/invoices/{invoice_id}", status_code=200)
async def delete_invoice(
    invoice_id: uuid.UUID,
    payload: InvoiceDeleteRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],
) -> dict:
    """Soft-delete via status CANCELLED. We DO NOT hard-delete — an
    invoice may have been shared or referenced externally. The Excel
    file on disk is preserved."""
    inv = await invoice_repo.get_by_id(db, invoice_id)
    if inv is None:
        raise NotFoundError(f"Invoice {invoice_id} not found.")
    if inv.status == InvoiceStatus.CANCELLED.value:
        raise ConflictError("Invoice is already cancelled.")

    inv.status = InvoiceStatus.CANCELLED.value
    # Note the reason in snapshot so future readers understand the state.
    snap = dict(inv.snapshot_data or {})
    snap["cancelled_reason"] = payload.reason
    snap["cancelled_by"] = admin.email
    from datetime import UTC, datetime
    snap["cancelled_at"] = datetime.now(UTC).isoformat()
    inv.snapshot_data = snap

    await db.commit()
    return {
        "id": str(inv.id),
        "status": inv.status,
        "message": "Invoice cancelled.",
    }


@router.post("/invoices/{invoice_id}/duplicate", status_code=201)
async def duplicate_invoice(
    invoice_id: uuid.UUID,
    payload: InvoiceDuplicateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],
) -> dict:
    """Create a new editable DRAFT from an approved invoice.

    Use case: same client asks for a recurring bill with slight tweaks —
    duplicate the last invoice, edit line items, submit for review again.
    Line items copy over exactly (including manual_override flags) so
    the reviewer sees a familiar starting point.
    """
    inv = await invoice_repo.get_by_id(db, invoice_id)
    if inv is None:
        raise NotFoundError(f"Invoice {invoice_id} not found.")

    snap = inv.snapshot_data or {}
    # Fresh invoice number so the new draft doesn't collide.
    new_invoice_no = await invoice_repo.next_invoice_number(db)

    from datetime import date
    from decimal import Decimal

    # Copy line_items EXACTLY — including `removed` flags so a duplicate
    # of an edited invoice starts from the same state the reviewer approved.
    lines = list(snap.get("line_items") or [])

    draft = await draft_repo.create(
        db,
        claim_id=inv.claim_id,
        client_id=inv.client_id,
        job_id=None,
        created_by=None,
        invoice_no=new_invoice_no,
        invoice_date=date.today(),
        gnc_file_no=snap.get("gnc_file_no", ""),
        client_details=snap.get("client_details") or {},
        insured_details=snap.get("insured_details") or {},
        loss_details=snap.get("loss_details") or {},
        line_items=lines,
        billing_period_start=inv.billing_period_start,
        billing_period_end=inv.billing_period_end,
        subtotal=Decimal(str(snap.get("subtotal") or 0)),
        grand_total=Decimal(str(snap.get("grand_total") or inv.amount)),
        currency=inv.currency,
        total_emails=snap.get("total_emails", 0),
        emails_reviewed=0,
    )

    # Record provenance in approval_history — reviewer sees "duplicated from X"
    hist = list(draft.approval_history or [])
    from datetime import UTC, datetime
    hist.append({
        "at": datetime.now(UTC).isoformat(),
        "action": "duplicated",
        "from_status": None,
        "to_status": "DRAFT",
        "user_id": admin.email,
        "note": payload.note or f"Duplicated from invoice {inv.invoice_no}",
        "change": {
            "source_invoice_id": str(inv.id),
            "source_invoice_no": inv.invoice_no,
        },
    })
    draft.approval_history = hist
    await db.commit()

    return {
        "draft_id": str(draft.id),
        "invoice_no": draft.invoice_no,
        "source_invoice_no": inv.invoice_no,
        "message": f"Draft {draft.invoice_no} created from {inv.invoice_no}.",
    }


# ===========================================================================
# Draft listing (global) — dashboard "Pending Review" section
# ===========================================================================
@router.get("/drafts")
async def list_drafts(
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
    status: str | None = None,
    pending: bool = False,          # convenience filter — all PENDING_* + DRAFT
    client_id: uuid.UUID | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """List drafts across all claims. `pending=true` returns everything
    that needs human review (DRAFT, PENDING_PM, PENDING_HOUR_VERIFY,
    PENDING_RS) — the exact set the dashboard's Pending Review card counts.
    """
    limit = max(1, min(limit, 100))
    offset = max(0, offset)

    statuses = None
    if pending:
        statuses = [
            DraftStatus.DRAFT.value,
            DraftStatus.PENDING_PM.value,
            DraftStatus.PENDING_HOUR_VERIFY.value,
            DraftStatus.PENDING_RS.value,
        ]

    from app.services import workflow_service
    rows, total = await draft_repo.list_paginated(
        db, status=status, statuses=statuses,
        client_id=client_id, limit=limit, offset=offset,
    )

    items = []
    for d in rows:
        items.append({
            "id": str(d.id),
            "invoice_no": d.invoice_no,
            "claim_id": str(d.claim_id),
            "client_id": str(d.client_id),
            "status": d.status,
            "stage": workflow_service.STAGE_LABEL.get(d.status, d.status),
            "grand_total": float(d.grand_total),
            "currency": d.currency,
            "created_at": d.created_at.isoformat() if d.created_at else None,
            # Denormalized labels for list display — spare the frontend a lookup.
            "client_name": (d.client_details or {}).get("name"),
            "insured_name": (d.insured_details or {}).get("insured_name"),
            "claim_no": (d.loss_details or {}).get("claim_no"),
            "gnc_file_no": d.gnc_file_no,
            "has_duplicate_warning": d.duplicate_warning is not None,
        })

    return {"total": total, "limit": limit, "offset": offset, "items": items}
