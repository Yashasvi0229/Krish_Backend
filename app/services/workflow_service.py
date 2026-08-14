"""
Multi-stage approval workflow.

State machine (see spec §12.2):

     DRAFT ─── submit ───▶ PENDING_PM
                              │
                              │ advance
                              ▼
                       PENDING_HOUR_VERIFY
                              │
                              │ advance
                              ▼
                         PENDING_RS
                              │
                              │ advance ─▶ APPROVED   (Excel generated,
                              │                        Invoice row created)
                              │
                    ┌─ reject at any pending stage ─┐
                    ▼                                ▼
                REJECTED  ── reopen ──▶ DRAFT       (terminal without reopen)

Design principles:
    * Only forward transitions are allowed from PENDING_* states — no
      skipping stages. Rejecting bounces to REJECTED with a mandatory
      reason.
    * Every transition appends to `approval_history`. That's the audit
      trail for both compliance and UI display ("who approved when").
    * Approving from PENDING_RS is the ONE stage that triggers side
      effects — Excel render + Invoice row insert. Everything before is
      pure status change + note.

Idempotency:
    * Submitting a draft that's already submitted → ConflictError
      (rather than silent success — reviewers should know).
    * Advancing an APPROVED/REJECTED draft → ConflictError.

Compatibility with existing `POST /api/drafts/{draft_id}/approve`:
    The old single-step approve remains valid (DRAFT → APPROVED directly)
    for callers that don't want the multi-stage flow. `approve_draft()`
    below is the new multi-stage path.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import DraftStatus
from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models.client import Client
from app.models.invoice_draft import InvoiceDraft
from app.repositories import draft_repo, invoice_repo

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Transition table
# ---------------------------------------------------------------------------
# `advance` from PENDING_RS is special: it becomes the APPROVE transition
# and triggers Excel generation + Invoice creation.
_ADVANCE_MAP: dict[str, str] = {
    DraftStatus.PENDING_PM.value:           DraftStatus.PENDING_HOUR_VERIFY.value,
    DraftStatus.PENDING_HOUR_VERIFY.value:  DraftStatus.PENDING_RS.value,
    DraftStatus.PENDING_RS.value:           DraftStatus.APPROVED.value,
}

# States from which the reviewer may bounce to REJECTED.
_REJECTABLE: frozenset[str] = frozenset({
    DraftStatus.PENDING_PM.value,
    DraftStatus.PENDING_HOUR_VERIFY.value,
    DraftStatus.PENDING_RS.value,
})

# Human-readable stage names for UI + audit entries.
STAGE_LABEL: dict[str, str] = {
    DraftStatus.DRAFT.value:                "Draft (editable)",
    DraftStatus.PENDING_PM.value:           "Pending PM Review",
    DraftStatus.PENDING_HOUR_VERIFY.value:  "Pending Hours Verification",
    DraftStatus.PENDING_RS.value:           "Pending RS (Final) Review",
    DraftStatus.APPROVED.value:             "Approved & Invoiced",
    DraftStatus.REJECTED.value:             "Rejected",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def submit_for_review(
    session: AsyncSession, draft_id: uuid.UUID,
    *, user_id: str | None, note: str | None = None,
) -> InvoiceDraft:
    """DRAFT → PENDING_PM. Called by the preparer after they finalize."""
    draft = await _load(session, draft_id)
    if draft.status != DraftStatus.DRAFT.value:
        raise ConflictError(
            f"Cannot submit — draft is in '{draft.status}'. Only DRAFT "
            f"drafts may be submitted for review."
        )

    active_lines = [li for li in (draft.line_items or []) if not li.get("removed")]
    if not active_lines:
        raise ConflictError(
            "Cannot submit an empty draft — add at least one line item first."
        )

    _transition(draft,
                to_status=DraftStatus.PENDING_PM.value,
                action="submitted", user_id=user_id, note=note)
    await session.flush()
    log.info("draft_submitted_for_review",
             draft_id=str(draft_id), user_id=str(user_id) if user_id else None)
    return draft


async def advance_stage(
    session: AsyncSession, draft_id: uuid.UUID,
    *, user_id: str | None, note: str | None = None,
    hourly_rate_for_final: Decimal = Decimal("150.00"),
) -> tuple[InvoiceDraft, uuid.UUID | None]:
    """Move the draft one stage forward.

    Returns (draft, invoice_id) — invoice_id is set ONLY on the final
    PENDING_RS → APPROVED transition, when the Excel gets rendered.
    """
    draft = await _load(session, draft_id)
    next_status = _ADVANCE_MAP.get(draft.status)
    if next_status is None:
        raise ConflictError(
            f"Cannot advance — draft is in '{draft.status}'. "
            f"Only PENDING_* drafts may be advanced."
        )

    invoice_id: uuid.UUID | None = None
    if next_status == DraftStatus.APPROVED.value:
        # Final approval → render Excel + create Invoice row.
        # Delayed import — invoice_service pulls in openpyxl which is
        # heavy at module import time on cold start.
        from app.services.invoice_service import (
            render_invoice_xlsx, save_invoice_file,
        )

        active_lines = [
            li for li in (draft.line_items or []) if not li.get("removed")
        ]
        # Guard: refuse to create an empty invoice. This is the last stop
        # before an Excel gets rendered and an Invoice row inserted — an
        # empty draft here would produce a $0 or broken invoice with no
        # billable content. Force the reviewer to add lines first.
        if not active_lines:
            raise ConflictError(
                "Cannot approve — draft has no billable line items. "
                "Add at least one line or reject this draft."
            )
        if float(draft.grand_total or 0) <= 0:
            raise ConflictError(
                "Cannot approve — draft total is zero or negative. "
                "Check the line items before finalizing."
            )

        client = await session.get(Client, draft.client_id)
        rate = _resolve_hourly_rate(client, hourly_rate_for_final)
        xlsx_bytes = render_invoice_xlsx(
            invoice_no=draft.invoice_no,
            invoice_date=draft.invoice_date,
            gnc_file_no=draft.gnc_file_no,
            client_details=draft.client_details,
            insured_details=draft.insured_details,
            loss_details=draft.loss_details,
            line_items=active_lines,
            billing_period_start=draft.billing_period_start,
            billing_period_end=draft.billing_period_end,
            subtotal=Decimal(str(draft.subtotal)),
            grand_total=Decimal(str(draft.grand_total)),
            hourly_rate=rate,
            currency=draft.currency,
        )
        rel_path, size = save_invoice_file(
            draft.invoice_no, draft.gnc_file_no, xlsx_bytes,
        )

        snapshot = _build_snapshot(draft, active_lines, note=note)
        invoice = await invoice_repo.create(
            session,
            draft_id=draft.id,
            claim_id=draft.claim_id,
            client_id=draft.client_id,
            invoice_no=draft.invoice_no,
            snapshot_data=snapshot,
            excel_path=rel_path,
            excel_file_size=size,
            amount=Decimal(str(draft.grand_total)),
            billing_period_start=draft.billing_period_start,
            billing_period_end=draft.billing_period_end,
            currency=draft.currency,
            approved_by=None,   # Phase 1: no user UUID; audit trail uses email
            emails_processed=draft.total_emails,
            attachments_count=sum(
                1 for li in active_lines if li.get("source_attachment_id")
            ),
            manual_overrides=sum(
                1 for li in active_lines if li.get("manual_override")
            ),
        )
        invoice_id = invoice.id
        draft.approved_invoice_id = invoice.id

    _transition(draft, to_status=next_status,
                action="approved" if next_status == DraftStatus.APPROVED.value
                                  else "advanced",
                user_id=user_id, note=note)
    await session.flush()
    log.info("draft_stage_advanced",
             draft_id=str(draft_id), new_status=next_status,
             invoice_id=str(invoice_id) if invoice_id else None)
    return draft, invoice_id


async def reject(
    session: AsyncSession, draft_id: uuid.UUID,
    *, user_id: str | None, reason: str, return_to_draft: bool = False,
) -> InvoiceDraft:
    """PENDING_X → REJECTED (with optional bounce back to DRAFT)."""
    draft = await _load(session, draft_id)
    if draft.status not in _REJECTABLE:
        raise ConflictError(
            f"Cannot reject — draft is in '{draft.status}'. "
            f"Only PENDING_* drafts may be rejected."
        )

    _transition(draft, to_status=DraftStatus.REJECTED.value,
                action="rejected", user_id=user_id, note=reason)
    draft.rejected_reason = reason

    if return_to_draft:
        # Immediately reopen — preparer can act on feedback right away.
        _transition(draft, to_status=DraftStatus.DRAFT.value,
                    action="reopened", user_id=user_id,
                    note="Auto-reopened after rejection")
        draft.rejected_reason = None

    await session.flush()
    log.info("draft_rejected", draft_id=str(draft_id),
             returned_to_draft=return_to_draft)
    return draft


async def reopen(
    session: AsyncSession, draft_id: uuid.UUID,
    *, user_id: str | None, note: str | None = None,
) -> InvoiceDraft:
    """REJECTED → DRAFT (admin action). Only allowed from REJECTED."""
    draft = await _load(session, draft_id)
    if draft.status != DraftStatus.REJECTED.value:
        raise ConflictError(
            f"Cannot reopen — draft is in '{draft.status}'. "
            f"Only REJECTED drafts may be reopened."
        )
    _transition(draft, to_status=DraftStatus.DRAFT.value,
                action="reopened", user_id=user_id, note=note)
    draft.rejected_reason = None
    await session.flush()
    log.info("draft_reopened", draft_id=str(draft_id))
    return draft


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
async def _load(session: AsyncSession, draft_id: uuid.UUID) -> InvoiceDraft:
    draft = await draft_repo.get_by_id(session, draft_id)
    if draft is None:
        raise NotFoundError(f"Draft {draft_id} not found.")
    return draft


def _transition(
    draft: InvoiceDraft, *,
    to_status: str, action: str,
    user_id: str | None, note: str | None,
) -> None:
    """Set status + append audit entry. Never mutates line_items."""
    from_status = draft.status
    draft.status = to_status
    hist = list(draft.approval_history or [])
    hist.append({
        "at": datetime.now(UTC).isoformat(),
        "action": action,
        "from_status": from_status,
        "to_status": to_status,
        "user_id": str(user_id) if user_id else None,
        "note": note,
        "change": None,
    })
    draft.approval_history = hist


def _build_snapshot(
    draft: InvoiceDraft, active_lines: list[dict[str, Any]], note: str | None,
) -> dict[str, Any]:
    return {
        "invoice_no": draft.invoice_no,
        "invoice_date": draft.invoice_date.isoformat(),
        "gnc_file_no": draft.gnc_file_no,
        "client_details": draft.client_details,
        "insured_details": draft.insured_details,
        "loss_details": draft.loss_details,
        "line_items": active_lines,
        "subtotal": float(draft.subtotal),
        "gst_percent": float(draft.gst_percent),
        "gst_value": float(draft.gst_value),
        "discount_amount": float(draft.discount_amount),
        "grand_total": float(draft.grand_total),
        "currency": draft.currency,
        "approval_history": list(draft.approval_history or []),
        "notes": note,
        "approved_from_draft_id": str(draft.id),
    }


def _resolve_hourly_rate(client: Client | None, fallback: Decimal) -> Decimal:
    """Same fallback logic as api/invoices.py — 0 or missing → default."""
    if client is None or not client.rate_config:
        return fallback
    raw = (client.rate_config or {}).get("hourly_rate")
    if raw is None:
        return fallback
    try:
        rate = Decimal(str(raw))
    except Exception:  # noqa: BLE001
        return fallback
    return rate if rate > 0 else fallback
