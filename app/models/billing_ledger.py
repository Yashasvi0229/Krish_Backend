"""
BillingLedger model — cumulative hours per client per year.

Used by the tiered rate strategy (e.g. Wynward) to compute the correct
rate for the current invoice: sum hours_billed for the client in the
current period_year, look up the tier in rate_config, apply it.

Also acts as a duplicate-invoice guard: before generating a new invoice
for the same claim + billing_period, we check the ledger to see if
overlapping hours were already billed.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.claim import Claim
    from app.models.client import Client
    from app.models.invoice import Invoice


class BillingLedger(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "billing_ledger"

    client_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="RESTRICT"),
        nullable=False,
    )
    claim_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("claims.id", ondelete="SET NULL"),
        nullable=True,
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("invoices.id", ondelete="CASCADE"),
        nullable=False,
    )

    period_year: Mapped[int] = mapped_column(Integer, nullable=False)
    hours_billed: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    rate_applied: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    tier_name: Mapped[str | None] = mapped_column(String(50), nullable=True)

    billed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # ---- Relationships ----
    client: Mapped["Client"] = relationship(back_populates="ledger_entries")
    claim: Mapped["Claim | None"] = relationship(back_populates="ledger_entries")
    invoice: Mapped["Invoice"] = relationship(back_populates="ledger_entries")

    __table_args__ = (
        CheckConstraint("hours_billed >= 0", name="hours_billed_non_negative"),
        CheckConstraint("rate_applied >= 0", name="rate_applied_non_negative"),
        CheckConstraint(
            "period_year >= 2000 AND period_year <= 2100",
            name="period_year_range",
        ),
        Index("ix_billing_ledger_client_id_period_year", "client_id", "period_year"),
        Index("ix_billing_ledger_invoice_id", "invoice_id"),
    )

    def __repr__(self) -> str:
        return f"<BillingLedger client={self.client_id} year={self.period_year} hrs={self.hours_billed}>"
