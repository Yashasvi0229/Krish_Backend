"""Request/response schemas for editing an invoice draft.

These are the payloads for the review UI: adjusting line items,
approving/rejecting through the multi-stage workflow, and inspecting
history. They deliberately mirror the internal shape of `line_items` in
InvoiceDraft so a frontend can round-trip changes without translation.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Line-item editing
# ---------------------------------------------------------------------------
class LineItemEdit(BaseModel):
    """PATCH payload — every field optional; only supplied fields change.

    Business rules:
        * If `quantity_hours` OR `rate` is edited, total is recomputed here.
        * If `rule_code` is changed, we DO NOT automatically re-run the
          rules engine — the reviewer is expected to enter the desired
          hours manually. This avoids surprising overrides.
        * Any successful edit flips `manual_override` to true (audit trail).
    """
    description: str | None = Field(default=None, max_length=500)
    category: str | None = Field(default=None, max_length=100)
    rule_code: str | None = Field(default=None, max_length=50)
    quantity: float | None = Field(default=None, ge=0)
    quantity_unit: str | None = Field(default=None, max_length=50)
    quantity_hours: float | None = Field(default=None, ge=0)
    rate: float | None = Field(default=None, ge=0)
    reason: str | None = Field(
        default=None,
        description="Why the reviewer made this change — appears in audit trail.",
        max_length=500,
    )


class LineItemAdd(BaseModel):
    """POST payload — reviewer manually adds a line the AI missed."""
    description: str = Field(min_length=1, max_length=500)
    category: str = Field(min_length=1, max_length=100)
    rule_code: str = Field(min_length=1, max_length=50)
    quantity: float = Field(ge=0, default=1.0)
    quantity_unit: str = Field(default="flat", max_length=50)
    quantity_hours: float = Field(ge=0)
    rate: float | None = Field(default=None, ge=0)  # falls back to draft's default rate
    reason: str | None = Field(default=None, max_length=500)


class LineItemDelete(BaseModel):
    """DELETE payload — reason is recommended for the audit trail."""
    reason: str | None = Field(
        default=None,
        description="Why the reviewer removed this line (e.g. 'duplicate of line 12').",
        max_length=500,
    )


# ---------------------------------------------------------------------------
# Multi-stage workflow
# ---------------------------------------------------------------------------
class SubmitForReviewRequest(BaseModel):
    """POST body when moving DRAFT → PENDING_PM."""
    note: str | None = Field(default=None, max_length=1000)


class AdvanceStageRequest(BaseModel):
    """POST body when moving PENDING_X → PENDING_(X+1) or PENDING_RS → APPROVED."""
    note: str | None = Field(default=None, max_length=1000)


class RejectRequest(BaseModel):
    """POST body when moving PENDING_X → REJECTED. Reason mandatory."""
    reason: str = Field(min_length=1, max_length=2000)
    return_to_draft: bool = Field(
        default=False,
        description=(
            "If true, immediately reopen the draft (REJECTED → DRAFT) so "
            "the preparer can edit and re-submit. If false, the draft "
            "stays REJECTED (terminal, requires admin to reopen)."
        ),
    )


# ---------------------------------------------------------------------------
# Approval history entry — matches app/services/workflow_service.py output
# ---------------------------------------------------------------------------
class ApprovalHistoryEntry(BaseModel):
    at: str                          # ISO datetime
    from_status: str
    to_status: str
    action: str                      # submitted / advanced / rejected / approved / edited
    user_id: str | None = None
    note: str | None = None
    # For line-item edits, `change` captures old→new deltas
    change: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Invoice history query params
# ---------------------------------------------------------------------------
class InvoiceListParams(BaseModel):
    """Query params for GET /api/invoices."""
    client_id: uuid.UUID | None = None
    claim_id: uuid.UUID | None = None
    status: str | None = None
    from_date: str | None = None      # ISO YYYY-MM-DD
    to_date: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class InvoiceListItem(BaseModel):
    """One row in the invoice-list response."""
    id: uuid.UUID
    invoice_no: str
    claim_id: uuid.UUID
    client_id: uuid.UUID
    amount: Decimal
    currency: str
    status: str
    billing_period_start: str
    billing_period_end: str
    approved_at: str
    # Denormalized for list-display convenience
    client_name: str | None = None
    claim_no: str | None = None
    gnc_file_no: str | None = None
    insured_name: str | None = None


class InvoiceListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[InvoiceListItem]
