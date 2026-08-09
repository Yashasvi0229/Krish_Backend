"""
Client model — insurance companies GNC invoices to.

Each client has its own rate strategy (flat / tiered / fee_budget) and its
own invoice Excel template. `rate_config` is JSONB — its shape depends on
`rate_strategy`. See spec §16.3 for examples.
"""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import ClientType, RateStrategy, enum_values
from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.billing_ledger import BillingLedger
    from app.models.claim import Claim
    from app.models.invoice import Invoice
    from app.models.invoice_draft import InvoiceDraft


class Client(UUIDPKMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "clients"

    # ---- Identity ----
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    company_legal_name: Mapped[str] = mapped_column(String(255), nullable=False)
    client_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default=ClientType.INSURANCE.value
    )
    logo_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- Contact ----
    primary_contact_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str] = mapped_column(String(50), nullable=False)
    address_line1: Mapped[str] = mapped_column(Text, nullable=False)
    address_line2: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- Billing ----
    rate_strategy: Mapped[str] = mapped_column(
        String(20), nullable=False, default=RateStrategy.FLAT.value
    )
    rate_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CAD")
    gst_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0")
    )
    discount_terms: Mapped[str | None] = mapped_column(Text, nullable=True)
    discount_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    discount_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ---- Invoice numbering ----
    invoice_prefix: Mapped[str | None] = mapped_column(String(20), nullable=True)
    invoice_start_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reset_yearly: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # ---- Template ----
    template_path: Mapped[str] = mapped_column(Text, nullable=False)

    # ---- Meta ----
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    internal_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- Relationships ----
    claims: Mapped[list["Claim"]] = relationship(back_populates="client")
    drafts: Mapped[list["InvoiceDraft"]] = relationship(back_populates="client")
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="client")
    ledger_entries: Mapped[list["BillingLedger"]] = relationship(back_populates="client")

    __table_args__ = (
        CheckConstraint(
            f"client_type IN ({', '.join(repr(v) for v in enum_values(ClientType))})",
            name="client_type_valid",
        ),
        CheckConstraint(
            f"rate_strategy IN ({', '.join(repr(v) for v in enum_values(RateStrategy))})",
            name="rate_strategy_valid",
        ),
        CheckConstraint("gst_percent >= 0 AND gst_percent <= 100", name="gst_percent_range"),
        Index("ix_clients_name", "name"),
        Index("ix_clients_active", "is_active", postgresql_where="is_active = TRUE"),
    )

    def __repr__(self) -> str:
        return f"<Client {self.name} ({self.rate_strategy})>"
