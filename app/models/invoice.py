"""
Invoice model — the finalized, approved invoice.

Immutable after approval — corrections require a new invoice. The full
draft state at approval time is frozen into `snapshot_data` (JSONB) so we
never lose the exact contents billed to the client, even if the underlying
client/claim/rules records change later.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import InvoiceStatus, enum_values
from app.models.base import Base, TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.billing_ledger import BillingLedger
    from app.models.claim import Claim
    from app.models.client import Client
    from app.models.user import User


class Invoice(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "invoices"

    # ---- Identifiers ----
    invoice_no: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    draft_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("invoice_drafts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    claim_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("claims.id", ondelete="RESTRICT"),
        nullable=False,
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # ---- Frozen snapshot ----
    snapshot_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # ---- Generated Excel ----
    excel_path: Mapped[str] = mapped_column(Text, nullable=False)
    excel_file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # ---- Money ----
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=InvoiceStatus.APPROVED.value
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CAD")
    billing_period_start: Mapped[date] = mapped_column(Date, nullable=False)
    billing_period_end: Mapped[date] = mapped_column(Date, nullable=False)

    # ---- Approval ----
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # ---- Delivery ----
    sent_to_client_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    email_recipient: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ---- Quality metrics (for reporting) ----
    ai_confidence_avg: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0")
    )
    emails_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attachments_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    manual_overrides: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # ---- Relationships ----
    claim: Mapped["Claim"] = relationship(back_populates="invoices")
    client: Mapped["Client"] = relationship(back_populates="invoices")
    approver: Mapped["User | None"] = relationship(
        back_populates="invoices_approved", foreign_keys=[approved_by]
    )
    sender: Mapped["User | None"] = relationship(foreign_keys=[sent_by])
    ledger_entries: Mapped[list["BillingLedger"]] = relationship(back_populates="invoice")

    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in enum_values(InvoiceStatus))})",
            name="status_valid",
        ),
        CheckConstraint(
            "billing_period_end >= billing_period_start",
            name="billing_period_ordered",
        ),
        CheckConstraint("amount >= 0", name="amount_non_negative"),
        Index("ix_invoices_claim_id", "claim_id"),
        Index("ix_invoices_client_id", "client_id"),
        Index("ix_invoices_status", "status"),
        Index("ix_invoices_approved_at", "approved_at"),
    )

    def __repr__(self) -> str:
        return f"<Invoice {self.invoice_no} {self.status} {self.amount} {self.currency}>"
