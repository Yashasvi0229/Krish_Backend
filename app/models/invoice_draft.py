"""
InvoiceDraft model — editable invoice before final approval.

Lifecycle (spec §12.2 approval workflow):
    DRAFT → PENDING_PM → PENDING_HOUR_VERIFY → PENDING_RS → APPROVED → (Invoice created)
                                                          ↘ REJECTED (loop back to DRAFT)

`approved_invoice_id` FK is added post-hoc in a later migration to break
the circular dependency with `invoices.draft_id`.

`line_items` shape — see spec §16.3 (JSONB structure). Each item has:
    id, line_number, date, description, category, rule_code,
    quantity_hours, rate, total, source_email_id, source_attachment_id,
    ai_confidence, is_ai_generated, is_flagged, manual_override, table_section
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import DraftStatus, enum_values
from app.models.base import Base, TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.claim import Claim
    from app.models.client import Client
    from app.models.processing_job import ProcessingJob
    from app.models.user import User


class InvoiceDraft(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "invoice_drafts"

    # ---- Ownership ----
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
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("processing_jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ---- Status ----
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=DraftStatus.DRAFT.value
    )

    # ---- Invoice header (editable during review) ----
    invoice_no: Mapped[str] = mapped_column(String(50), nullable=False)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    gnc_file_no: Mapped[str] = mapped_column(String(50), nullable=False)

    # ---- Snapshots at draft time (JSONB — decouples from parent tables) ----
    client_details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    insured_details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    loss_details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # ---- Line items (see docstring for shape) ----
    line_items: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )

    # ---- Billing period ----
    billing_period_start: Mapped[date] = mapped_column(Date, nullable=False)
    billing_period_end: Mapped[date] = mapped_column(Date, nullable=False)

    # ---- Totals ----
    subtotal: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0")
    )
    gst_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0")
    )
    gst_value: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0")
    )
    discount_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0")
    )
    grand_total: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, default=Decimal("0")
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CAD")

    # ---- Review meta ----
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    emails_reviewed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_emails: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_warning: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # ---- Approval trail — array of {stage, actor, at, note} objects ----
    approval_history: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    rejected_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- FK to the finalized invoice (added by later ALTER migration) ----
    approved_invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )

    # ---- Relationships ----
    claim: Mapped["Claim"] = relationship(back_populates="drafts")
    client: Mapped["Client"] = relationship(back_populates="drafts")
    job: Mapped["ProcessingJob | None"] = relationship(back_populates="drafts")
    creator: Mapped["User | None"] = relationship(
        back_populates="drafts_created", foreign_keys=[created_by]
    )

    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in enum_values(DraftStatus))})",
            name="status_valid",
        ),
        CheckConstraint(
            "billing_period_end >= billing_period_start",
            name="billing_period_ordered",
        ),
        CheckConstraint("subtotal >= 0 AND grand_total >= 0", name="totals_non_negative"),
        Index("ix_invoice_drafts_claim_id", "claim_id"),
        Index("ix_invoice_drafts_client_id", "client_id"),
        Index("ix_invoice_drafts_status", "status"),
        Index("ix_invoice_drafts_created_by", "created_by"),
    )

    def __repr__(self) -> str:
        return f"<InvoiceDraft {self.invoice_no} status={self.status}>"
