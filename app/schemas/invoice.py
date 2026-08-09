"""Pydantic schemas for invoice + analyze endpoints."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.billing import BillingLineItem


# ---- Analyze -----------------------------------------------------------
class AnalyzeClaimRequest(BaseModel):
    """POST /api/claims/{claim_id}/analyze."""
    force_refresh: bool = Field(
        default=False,
        description="Ignore cached ai_analyses and rerun. Costs money."
    )


# ---- Draft view --------------------------------------------------------
class InvoiceDraftDetail(BaseModel):
    id: uuid.UUID
    claim_id: uuid.UUID
    client_id: uuid.UUID
    status: str
    invoice_no: str
    invoice_date: date
    gnc_file_no: str
    client_details: dict[str, Any]
    insured_details: dict[str, Any]
    loss_details: dict[str, Any]
    line_items: list[dict[str, Any]]
    billing_period_start: date
    billing_period_end: date
    subtotal: Decimal
    grand_total: Decimal
    currency: str
    total_emails: int
    emails_reviewed: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ---- Invoice generation ------------------------------------------------
class GenerateInvoiceRequest(BaseModel):
    """POST /api/drafts/{draft_id}/approve."""
    approve_notes: str | None = Field(default=None, max_length=2000)


class InvoiceDetail(BaseModel):
    id: uuid.UUID
    invoice_no: str
    claim_id: uuid.UUID
    client_id: uuid.UUID
    status: str
    amount: Decimal
    currency: str
    billing_period_start: date
    billing_period_end: date
    approved_at: datetime
    excel_download_url: str

    model_config = {"from_attributes": True}
