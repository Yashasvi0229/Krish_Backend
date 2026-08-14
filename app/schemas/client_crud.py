"""Request schemas for Client CRUD.

Kept separate from any auto-generated response schema so the API surface
stays intentional — we don't accidentally expose or accept internal
fields (like `deleted_at`) via loose Pydantic model_dump round-trips.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, EmailStr, Field


class ClientCreate(BaseModel):
    """POST /api/clients — all essential fields required."""
    name: str = Field(min_length=1, max_length=255)
    company_legal_name: str = Field(min_length=1, max_length=255)
    client_type: Literal["Insurance", "Adjuster", "Contractor", "Other"] = "Adjuster"
    primary_contact_name: str = Field(min_length=1, max_length=255)
    email: EmailStr
    phone: str = Field(min_length=1, max_length=50)
    address_line1: str = Field(min_length=1)
    address_line2: str | None = None
    hourly_rate: Decimal = Field(default=Decimal("150.00"), ge=0)
    currency: str = Field(default="CAD", min_length=3, max_length=3)
    gst_percent: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    invoice_prefix: str | None = Field(default=None, max_length=20)


class ClientUpdate(BaseModel):
    """PATCH /api/clients/{id} — every field optional.

    Note: `email` uses EmailStr for validation but we default None so we
    can distinguish "omit" from "set to null". Backend applies only the
    supplied keys — nothing else is touched.
    """
    name: str | None = Field(default=None, min_length=1, max_length=255)
    company_legal_name: str | None = Field(default=None, min_length=1, max_length=255)
    client_type: Literal["Insurance", "Adjuster", "Contractor", "Other"] | None = None
    primary_contact_name: str | None = Field(default=None, min_length=1, max_length=255)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, min_length=1, max_length=50)
    address_line1: str | None = None
    address_line2: str | None = None
    hourly_rate: Decimal | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    gst_percent: Decimal | None = Field(default=None, ge=0, le=100)
    invoice_prefix: str | None = Field(default=None, max_length=20)
    is_active: bool | None = None
