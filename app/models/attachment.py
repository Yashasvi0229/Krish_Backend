"""
Attachment model — files attached to Gmail messages.

Dedup by `file_hash` (SHA256 of the raw bytes). The same PDF attached to
5 emails is stored on disk exactly once; the DB has 5 attachment rows all
pointing to the same `storage_path`. This saves both disk and AI cost
(we only summarize each unique file once).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CHAR,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import ExtractionStatus, enum_values
from app.models.base import Base, TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.ai_analysis import AIAnalysis
    from app.models.email import Email


class Attachment(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "attachments"

    # ---- Ownership ----
    email_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("emails.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ---- File identity ----
    file_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_extension: Mapped[str] = mapped_column(String(20), nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)

    # ---- Extracted text (from PDF / DOCX / XLSX / OCR) ----
    extracted_text_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_text_snippet: Mapped[str | None] = mapped_column(Text, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ocr_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    extraction_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ExtractionStatus.PENDING.value
    )
    extraction_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- AI summary of this attachment (cached by input_hash on ai_analyses side) ----
    ai_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_type: Mapped[str | None] = mapped_column(String(100), nullable=True)

    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- Relationships ----
    email: Mapped["Email"] = relationship(back_populates="attachments")
    ai_analyses: Mapped[list["AIAnalysis"]] = relationship(back_populates="attachment")

    __table_args__ = (
        CheckConstraint(
            f"extraction_status IN ({', '.join(repr(v) for v in enum_values(ExtractionStatus))})",
            name="extraction_status_valid",
        ),
        CheckConstraint("file_size >= 0", name="file_size_non_negative"),
        Index("ix_attachments_file_hash", "file_hash"),
        Index("ix_attachments_email_id", "email_id"),
        Index("ix_attachments_extraction_status", "extraction_status"),
    )

    def __repr__(self) -> str:
        return f"<Attachment {self.filename} ({self.file_extension})>"
