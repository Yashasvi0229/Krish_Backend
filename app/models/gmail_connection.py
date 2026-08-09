"""
GmailConnection — singleton table for the shared GNC Gmail account.

Design note: per the product decision on 2026-07-28, the whole platform
reads from ONE Gmail account. Rather than overloading `users.google_refresh_token`
for a platform-level credential, we put it in its own row here.

Invariant: at most one row. Enforced by the CHECK constraint on `singleton_key`
which must always equal 'default'. The UNIQUE constraint on `singleton_key`
prevents duplicates.

Rotate/replace by UPSERTing on `singleton_key = 'default'`.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPKMixin


class GmailConnection(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "gmail_connection"

    # Always 'default' — used to enforce the singleton invariant via UNIQUE.
    singleton_key: Mapped[str] = mapped_column(
        String(20), nullable=False, unique=True, default="default"
    )

    # ---- Google identity ----
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    google_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ---- OAuth credentials (encrypted at rest via Fernet — see core/security.py) ----
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    access_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_expiry: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    scopes: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="openid email profile https://www.googleapis.com/auth/gmail.readonly",
    )

    # ---- Sync state ----
    is_connected: Mapped[bool] = mapped_column(nullable=False, default=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_history_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    connected_by_admin_email: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "singleton_key = 'default'", name="singleton_key_locked"
        ),
    )

    def __repr__(self) -> str:
        return f"<GmailConnection {self.email} connected={self.is_connected}>"
