"""
BillingRule model — data-driven billing rules from the Internal Hours
Allocation Guidelines.

Rules can be global (apply to all clients) or client_specific. Client-specific
rules override globals when scoring an email/attachment.

Versioning: every UPDATE bumps `version`. `rule_history` (added later) stores
prior versions so we can answer "which rule text was applied to invoice X?"
at any point in the past.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import ChargeType, ClientScope, UOM, enum_values
from app.models.base import Base, TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.user import User


class BillingRule(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "billing_rules"

    # ---- Identity ----
    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    # ---- Charge shape ----
    charge_type: Mapped[str] = mapped_column(String(20), nullable=False)
    base_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)
    flat_fee: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    uom: Mapped[str] = mapped_column(String(30), nullable=False)

    # ---- Conditions (e.g. {"max_lines": 2} or {"per_floor": true, "standard_sqft": 2500}) ----
    conditions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    # ---- Scope ----
    client_scope: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ClientScope.GLOBAL.value
    )
    client_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False, default=list
    )

    # ---- Human notes ----
    comments: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # ---- Versioning & audit ----
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ---- Relationships ----
    creator: Mapped["User | None"] = relationship()

    __table_args__ = (
        CheckConstraint(
            f"charge_type IN ({', '.join(repr(v) for v in enum_values(ChargeType))})",
            name="charge_type_valid",
        ),
        CheckConstraint(
            f"uom IN ({', '.join(repr(v) for v in enum_values(UOM))})",
            name="uom_valid",
        ),
        CheckConstraint(
            f"client_scope IN ({', '.join(repr(v) for v in enum_values(ClientScope))})",
            name="client_scope_valid",
        ),
        # For hourly rules, base_hours must be set. For flat_fee, flat_fee must be set.
        CheckConstraint(
            "(charge_type = 'hourly' AND base_hours IS NOT NULL) OR "
            "(charge_type = 'flat_fee' AND flat_fee IS NOT NULL) OR "
            "(charge_type = 'per_unit')",
            name="charge_config_present",
        ),
        Index("ix_billing_rules_active", "is_active", postgresql_where="is_active = TRUE"),
        Index("ix_billing_rules_client_scope", "client_scope"),
    )

    def __repr__(self) -> str:
        return f"<BillingRule {self.code} ({self.charge_type})>"
