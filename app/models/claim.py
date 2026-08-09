"""
Claim model — an insurance claim being invoiced against.

Uniqueness: (client_id, claim_no) is the natural key. `gnc_file_no` is also
unique globally — it's GNC's internal reference number visible on invoices.
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import ClaimStatus, enum_values
from app.models.base import Base, TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.billing_ledger import BillingLedger
    from app.models.client import Client
    from app.models.email import Email
    from app.models.invoice import Invoice
    from app.models.invoice_draft import InvoiceDraft


class Claim(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "claims"

    # ---- Identifiers ----
    gnc_file_no: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    claim_no: Mapped[str] = mapped_column(String(100), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # ---- Client ----
    # RESTRICT — don't allow deleting a client if claims still exist.
    client_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("clients.id", ondelete="RESTRICT"),
        nullable=False,
    )

    # ---- Insured details (name, addresses, contact) ----
    insured_details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )

    # ---- Loss info ----
    loss_type: Mapped[str] = mapped_column(String(50), nullable=False)
    date_of_loss: Mapped[date | None] = mapped_column(Date, nullable=True)
    file_type: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # ---- Status ----
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ClaimStatus.ACTIVE.value
    )

    # ---- Relationships ----
    client: Mapped["Client"] = relationship(back_populates="claims")
    emails: Mapped[list["Email"]] = relationship(back_populates="claim")
    drafts: Mapped[list["InvoiceDraft"]] = relationship(back_populates="claim")
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="claim")
    ledger_entries: Mapped[list["BillingLedger"]] = relationship(back_populates="claim")

    __table_args__ = (
        # Same claim_no can appear under different clients — uniqueness is per-client.
        UniqueConstraint("client_id", "claim_no", name="uq_claims_client_id_claim_no"),
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in enum_values(ClaimStatus))})",
            name="status_valid",
        ),
        Index("ix_claims_file_name", "file_name"),
        Index("ix_claims_client_id", "client_id"),
    )

    def __repr__(self) -> str:
        return f"<Claim {self.gnc_file_no} / {self.claim_no}>"
