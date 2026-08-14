"""Request schemas for BillingRule CRUD."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field


class RuleCreate(BaseModel):
    """POST /api/rules"""
    code: str = Field(min_length=1, max_length=50)
    category: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1)
    charge_type: Literal["hourly", "flat"] = "hourly"
    base_hours: Decimal | None = Field(default=None, ge=0)
    flat_fee: Decimal | None = Field(default=None, ge=0)
    uom: str = Field(min_length=1, max_length=30)
    conditions: dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True


class RuleUpdate(BaseModel):
    """PATCH /api/rules/{id} — every field optional."""
    code: str | None = Field(default=None, min_length=1, max_length=50)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = None
    charge_type: Literal["hourly", "flat"] | None = None
    base_hours: Decimal | None = Field(default=None, ge=0)
    flat_fee: Decimal | None = Field(default=None, ge=0)
    uom: str | None = Field(default=None, min_length=1, max_length=30)
    conditions: dict[str, Any] | None = None
    is_active: bool | None = None
