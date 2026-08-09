"""
Email model — every Gmail message fetched from the shared GNC mailbox.

Deduplication (spec §16.3):
    1. Primary key against re-processing: UNIQUE(gmail_message_id).
       If we see the same Gmail message twice, we skip inserting.
    2. Secondary content-level dedup: `content_hash` is SHA256 over
       (subject + body + from + date). If two different Gmail messages
       carry the same content, we can reuse the previous AI analysis
       instead of paying for another AI call.

`ai_analysis_id` FK is added post-hoc in a later migration to break the
circular dependency with `ai_analyses.email_id`.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CHAR,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.ai_analysis import AIAnalysis
    from app.models.attachment import Attachment
    from app.models.claim import Claim


class Email(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "emails"

    # ---- Gmail identifiers ----
    gmail_message_id: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    gmail_thread_id: Mapped[str] = mapped_column(String(255), nullable=False)
    gmail_link: Mapped[str] = mapped_column(Text, nullable=False)

    # ---- Claim link — SET NULL so we don't lose emails if a claim is deleted ----
    claim_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("claims.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ---- Dedup ----
    content_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)

    # ---- Headers ----
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    from_email: Mapped[str] = mapped_column(String(255), nullable=False)
    from_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    to_emails: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    cc_emails: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list
    )
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # ---- Body storage ----
    # Full body lives on disk (path). Snippet in DB for search & display.
    body_path: Mapped[str] = mapped_column(Text, nullable=False)
    body_snippet: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # ---- Meta ----
    is_internal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- AI analysis link — FK constraint added in a later ALTER migration
    # to avoid the circular FK with ai_analyses.email_id at initial creation.
    ai_analysis_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )

    # ---- Relationships ----
    claim: Mapped["Claim | None"] = relationship(back_populates="emails")
    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="email", cascade="all, delete-orphan"
    )
    # This relationship uses foreign_keys="AIAnalysis.email_id" — the reverse side.
    ai_analyses: Mapped[list["AIAnalysis"]] = relationship(
        back_populates="email",
        foreign_keys="AIAnalysis.email_id",
    )

    __table_args__ = (
        Index("ix_emails_content_hash", "content_hash"),
        Index("ix_emails_claim_id", "claim_id"),
        Index("ix_emails_gmail_thread_id", "gmail_thread_id"),
        Index("ix_emails_date", "date", postgresql_using="btree"),
    )

    def __repr__(self) -> str:
        return f"<Email {self.gmail_message_id} from={self.from_email!r}>"
