"""Pydantic schemas for billing line items."""
from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, Field


class BillingLineItem(BaseModel):
    """One line on the invoice draft."""
    line_number: int
    description: str
    category: str
    rule_code: str
    quantity: float
    quantity_unit: str
    quantity_hours: float
    rate: float
    total: float
    source_email_id: uuid.UUID | None = None
    source_attachment_id: uuid.UUID | None = None
    ai_confidence: str = "Medium"    # High / Medium / Low
    ai_reasoning: str | None = None
    is_flagged: bool = False
    flag_reason: str | None = None
    hit_cap: bool = False
    manual_override: bool = False
