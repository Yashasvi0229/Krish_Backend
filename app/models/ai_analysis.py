"""
AIAnalysis model — cached AI outputs (classifications, summaries, hour
recommendations, description rewrites).

Cache key: `input_hash` = SHA256(input_content + prompt_version + model).
Before calling AI, services look up `input_hash`; on hit they reuse the
stored result. On prompt_version bump (breaking prompt change), all cached
rows are logically invalidated automatically because the hash changes.

Cost tracking: `input_tokens`, `output_tokens`, `cost_usd`, `latency_ms`
enable per-invoice AI cost reports (spec §26).
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CHAR,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import AIProvider, AnalysisType, Confidence, enum_values
from app.models.base import Base, TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.attachment import Attachment
    from app.models.email import Email


class AIAnalysis(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "ai_analyses"

    # ---- Target — exactly one of email_id / attachment_id must be set.
    # Enforced by a CHECK constraint below.
    email_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("emails.id", ondelete="CASCADE"),
        nullable=True,
    )
    attachment_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("attachments.id", ondelete="CASCADE"),
        nullable=True,
    )

    # ---- Cache key ----
    input_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False, unique=True)

    # ---- Analysis metadata ----
    analysis_type: Mapped[str] = mapped_column(String(50), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(20), nullable=False)

    # ---- Classification result ----
    is_billable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rule_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    recommended_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    confidence: Mapped[str] = mapped_column(
        String(10), nullable=False, default=Confidence.MEDIUM.value
    )

    # ---- Text outputs ----
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- Flagging (AI unsure → needs human review) ----
    should_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    flag_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ---- Cost / observability ----
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0")
    )
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    raw_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # ---- Relationships ----
    email: Mapped["Email | None"] = relationship(
        back_populates="ai_analyses", foreign_keys=[email_id]
    )
    attachment: Mapped["Attachment | None"] = relationship(back_populates="ai_analyses")

    __table_args__ = (
        CheckConstraint(
            "(email_id IS NOT NULL) <> (attachment_id IS NOT NULL)",
            name="target_exactly_one",
        ),
        CheckConstraint(
            f"analysis_type IN ({', '.join(repr(v) for v in enum_values(AnalysisType))})",
            name="analysis_type_valid",
        ),
        CheckConstraint(
            f"provider IN ({', '.join(repr(v) for v in enum_values(AIProvider))})",
            name="provider_valid",
        ),
        CheckConstraint(
            f"confidence IN ({', '.join(repr(v) for v in enum_values(Confidence))})",
            name="confidence_valid",
        ),
        Index("ix_ai_analyses_email_id", "email_id"),
        Index("ix_ai_analyses_attachment_id", "attachment_id"),
    )

    def __repr__(self) -> str:
        target = f"email={self.email_id}" if self.email_id else f"att={self.attachment_id}"
        return f"<AIAnalysis {self.analysis_type} {target} conf={self.confidence}>"
