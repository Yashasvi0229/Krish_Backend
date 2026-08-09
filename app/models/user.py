"""
User model.

Represents an authenticated platform user. Per current product decisions
(see conversation on 2026-07-28), Phase 1 login uses hardcoded admin creds
from env vars and does NOT insert into this table. In Phase 2 (multi-user)
we start creating rows here via a register endpoint.

The `google_refresh_token` field remains on the user row per spec §16.3
so we can later support per-user Gmail connections. For Phase 1 the shared
GNC Gmail account lives in the `gmail_connection` singleton table instead.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import UserRole, enum_values
from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.audit_log import AuditLog
    from app.models.invoice import Invoice
    from app.models.invoice_draft import InvoiceDraft
    from app.models.processing_job import ProcessingJob


class User(UUIDPKMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "users"

    # ---- Identity ----
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- Auth ----
    # Nullable because Phase 1 admin creds live in env vars, not DB.
    # Phase 2 (register endpoint) will populate this via passlib bcrypt.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    role: Mapped[str] = mapped_column(
        String(20), nullable=False, default=UserRole.USER.value
    )

    # ---- Optional Google linkage (per-user Gmail — Phase 2+) ----
    google_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    google_token_expiry: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- Status ----
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- Relationships (lazy — only loaded on access) ----
    processing_jobs: Mapped[list["ProcessingJob"]] = relationship(
        back_populates="user", cascade="save-update"
    )
    drafts_created: Mapped[list["InvoiceDraft"]] = relationship(
        back_populates="creator",
        foreign_keys="InvoiceDraft.created_by",
    )
    invoices_approved: Mapped[list["Invoice"]] = relationship(
        back_populates="approver",
        foreign_keys="Invoice.approved_by",
    )
    audit_logs: Mapped[list["AuditLog"]] = relationship(back_populates="user")

    # ---- Constraints & indexes ----
    __table_args__ = (
        CheckConstraint(
            f"role IN ({', '.join(repr(v) for v in enum_values(UserRole))})",
            name="role_valid",
        ),
        Index("ix_users_role", "role"),
        Index(
            "ix_users_active",
            "is_active",
            postgresql_where="is_active = TRUE",
        ),
    )

    def __repr__(self) -> str:
        return f"<User {self.email} ({self.role})>"
